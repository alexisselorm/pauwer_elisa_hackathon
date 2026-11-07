"""ContextRepository: the one place that reads and writes the context layer in Neo4j.

Reads implement the as-of rules (plan 001 R9/R10):
  in scope at t   <=> valid_from <= t < coalesce(valid_to, inf)
  known at k      <=> recorded_at <= k and (retracted_at is null or retracted_at > k)

Writes (agents, via MCP) run in one transaction each and are idempotent: a WriteReceipt node
keyed by agent, tool and idempotency key stores the first result, and a replay returns it.
Every write also records an AgentAction event (R1: every write is itself an event).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, LiteralString

from neo4j import AsyncDriver, AsyncManagedTransaction
from neo4j.exceptions import ConstraintError
from neo4j.time import DateTime as Neo4jDateTime
from pydantic import ValidationError

from silentctx.context.models import (
    AccessRecordView,
    AssertionIn,
    AssertionOut,
    AssertionView,
    EntityView,
    EventView,
    EvidenceView,
    FlagView,
    Match,
    SegmentRef,
    SourceIn,
)
from silentctx.domain import time as rules
from silentctx.domain.models import AssertionStatus
from silentctx.errors import WriteRejectedError
from silentctx.graph.cypher import vector

END_OF_TIME: LiteralString = "datetime('9999-12-31T00:00:00Z')"

# Ranking (plan 002 §4.1 "ranked"): how the assertion reached the entity, its status and its
# confidence. Shown to agents as `score`; never used to hide anything.
MATCH_WEIGHT = {
    Match.DIRECT: 1.0,
    Match.MODEL: 0.9,
    Match.SITE: 0.85,
    Match.SEGMENT: 0.8,
    Match.SAME_MODEL_DEVICE: 0.5,
}
STATUS_WEIGHT = {
    AssertionStatus.BELIEVED_TRUE: 1.0,
    AssertionStatus.UNVERIFIED: 0.8,
    AssertionStatus.DISPUTED: 0.7,
    # Everything ranked has already passed the validity filter at t (R9, R12 plan 004), so
    # "superseded" / "expired" only say what happened *after* t. At a past as_of that
    # assertion is the one in force; a penalty would push the right answer down.
    AssertionStatus.SUPERSEDED: 1.0,
    AssertionStatus.EXPIRED: 1.0,
}
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.85, "low": 0.6}
SCOPE_BY_ENTITY_TYPE = {
    "device": "observed_on",
    "fibre": "observed_on",
    "microwave": "observed_on",
    "site": "observed_on",
    "hw_model": "applies_to",
    "generation": "applies_to",
    "topic": "activity",
}


class NotFoundError(LookupError):
    """An id that doesn't exist (entity, assertion, event)."""


def py(value: Any) -> Any:
    """Neo4j values -> plain Python (DateTime -> aware datetime), recursively."""
    if isinstance(value, Neo4jDateTime):
        return value.to_native()
    if isinstance(value, dict):
        return {k: py(v) for k, v in value.items()}
    if isinstance(value, list):
        return [py(v) for v in value]
    return value


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


# Projection of one assertion with everything an agent needs to cite and judge it.
_ASSERTION_FIELDS: LiteralString = """
  a { .* , embedding: null } AS a,
  [(a)-[b:ABOUT]->(e:Entity) | {entity_id: e.entity_id, scope: b.scope,
      candidate: coalesce(b.candidate, false)}] AS bindings,
  [(a)-[:CONTRADICTS]-(x:ContextAssertion) | x.assertion_id] AS contradicts,
  [(a)-[:CORROBORATES]-(x:ContextAssertion) | x.assertion_id] AS corroborates,
  head([(x:ContextAssertion)-[:SUPERSEDES]->(a) WHERE x.retracted_at IS NULL | x.assertion_id]
    + [(x:ContextAssertion)-[:SUPERSEDES]->(a) | x.assertion_id]) AS superseded_by,
  [(x:ContextAssertion)-[:SUPERSEDES]->(a) | x { .assertion_id, .recorded_at,
      .retracted_at }] AS successors,
  head([(a)-[:SUPERSEDES]->(x:ContextAssertion) WHERE x.retracted_at IS NULL | x.assertion_id]
    + [(a)-[:SUPERSEDES]->(x:ContextAssertion) | x.assertion_id]) AS supersedes,
  [(a)-[:DERIVED_FROM]->(ev:Event) | ev { .event_id, .event_time, .platform, .author,
      .channel, .permalink, .text }] AS sources
"""

# V3: a candidate binding (ambiguous, or not alive at valid_from) does not put an assertion
# in an entity's scope until a person picks it on the Slack card.
_CONFIRMED: LiteralString = "NOT coalesce(r.candidate, false)"
_KNOWN: LiteralString = "a.recorded_at <= $k AND (a.retracted_at IS NULL OR a.retracted_at > $k)"
# R10: validity as it was known at k. A supersession holds while its successor is live:
# on record by k (R2) and not retracted by k (R6). With no live successor the predecessor
# is open-ended at k: before the successor was recorded ("why did the AI dispatch to Kemi
# on 08-10 with the padlock key?"), and again after the successor is retracted (BE-1.8).
# A corrected successor hands over to its fix at the same instant, so there is no gap.
# The stored valid_to stays on record; it is only applied while a successor is live.
_LIVE_SUCC: LiteralString = (
    "succ.recorded_at <= $k AND (succ.retracted_at IS NULL OR succ.retracted_at > $k)"
)
_VALID_TO_AT_K: LiteralString = (
    "CASE WHEN EXISTS { MATCH (:ContextAssertion)-[:SUPERSEDES]->(a) } AND NOT EXISTS { "
    "MATCH (succ:ContextAssertion)-[:SUPERSEDES]->(a) WHERE " + _LIVE_SUCC + " } "
    "THEN null ELSE a.valid_to END"
)
_VALID: LiteralString = (
    "a.valid_from <= $t AND $t < coalesce(" + _VALID_TO_AT_K + ", " + END_OF_TIME + ")"
)


def assertion_view(
    record: Mapping[str, Any], match: Match | None = None, known_at: datetime | None = None
) -> AssertionView:
    """One assertion, as it was on record at `known_at` (R10; None = as stored now). A
    supersession applies only while its successor is live: recorded by then and not yet
    retracted (BE-1.8). Otherwise the assertion is open-ended with its own status."""
    a = py(dict(record["a"]))
    superseded_by = record.get("superseded_by")
    successors = py(record.get("successors") or [])
    if successors:
        live = sorted(
            (
                s
                for s in successors
                if (known_at is None or s["recorded_at"] <= known_at)
                and (
                    s.get("retracted_at") is None
                    or (known_at is not None and s["retracted_at"] > known_at)
                )
            ),
            key=lambda s: s["recorded_at"],
        )
        if live:
            superseded_by = live[-1]["assertion_id"]
            if a.get("status") != "expired":
                a["status"] = "superseded"
        else:
            a["valid_to"] = None
            if a.get("status") == "superseded":
                a["status"] = a.get("status_before") or "believed_true"
            superseded_by = None
    qualifiers = json.loads(a.get("qualifiers_json") or "{}")
    citations = []
    if a.get("source_type") or a.get("source_ref"):
        citations.append(
            {
                "source_type": a.get("source_type"),
                "source_ref": a.get("source_ref"),
                "detail": a.get("source_detail"),
                "author": a.get("asserted_by"),
                "recorded_at": a.get("recorded_at"),
            }
        )
    # The message that made it knowledge (recorded_at) is cited first; the rest follow.
    sources = sorted(
        py(record.get("sources") or []),
        key=lambda src: (src.get("event_time") != a.get("recorded_at"), str(src.get("event_time"))),
    )
    for src in sources:
        citations.append(
            {
                "source_type": src.get("platform"),
                "source_ref": src.get("permalink") or src.get("event_id"),
                "detail": (src.get("text") or "")[:200] or src.get("channel"),
                "author": src.get("author"),
                "recorded_at": src.get("event_time"),
            }
        )
    confidence = a.get("confidence")
    if confidence is None:
        confidence = a.get("confidence_label")
    view = AssertionView.model_validate(
        {
            "assertion_id": a["assertion_id"],
            "statement": a.get("statement") or "",
            "topic": a.get("topic"),
            "status": a.get("status") or "unverified",
            "origin": a.get("origin"),
            "confidence": confidence,
            "valid_from": a["valid_from"],
            "valid_to": a.get("valid_to"),
            "recorded_at": a["recorded_at"],
            "retracted_at": a.get("retracted_at"),
            "review_due": bool(a.get("review_due")),
            "support_n": int(a.get("support_n") or 0),
            "refute_n": int(a.get("refute_n") or 0),
            "qualifiers": qualifiers,
            "bindings": record.get("bindings") or [],
            "citations": citations,
            "contradicts": sorted(set(record.get("contradicts") or [])),
            "corroborates": sorted(set(record.get("corroborates") or [])),
            "superseded_by": superseded_by,
            "supersedes": record.get("supersedes"),
            "match": match,
        }
    )
    view.score = round(score(view), 3)
    return view


def score(view: AssertionView) -> float:
    conf = view.confidence
    if isinstance(conf, int | float):
        conf_w = 0.5 + 0.5 * float(conf)
    else:
        conf_w = CONFIDENCE_WEIGHT.get(str(conf), 0.8)
    s = MATCH_WEIGHT.get(view.match, 0.7) if view.match else 0.7
    s *= STATUS_WEIGHT.get(view.status, 0.7) * conf_w
    if view.review_due:
        s *= 0.9
    return s


class ContextRepository:
    def __init__(self, driver: AsyncDriver, database: str) -> None:
        self._driver = driver
        self._db = database

    async def _read(self, query: LiteralString, **params: Any) -> list[dict[str, Any]]:
        records, _, _ = await self._driver.execute_query(
            query, params, database_=self._db, routing_="r"
        )
        return [dict(r) for r in records]

    # --- Entities -------------------------------------------------------------------------

    async def all_entities(self) -> list[dict[str, Any]]:
        rows = await self._read(
            "MATCH (e:Entity) RETURN e.entity_id AS entity_id, e.entity_type AS entity_type, "
            "e.name AS name, e.city AS city, e.room AS room, e.label AS label, "
            "e.model AS model, e.private_road AS private_road, "
            "e.generation AS generation, e.commissioned AS commissioned, "
            "e.valid_from AS valid_from, e.valid_to AS valid_to"
        )
        return [py(r) for r in rows]

    async def entity_types(self, ids: Sequence[str]) -> dict[str, str]:
        rows = await self._read(
            "MATCH (e:Entity) WHERE e.entity_id IN $ids RETURN e.entity_id AS id, "
            "e.entity_type AS type",
            ids=list(ids),
        )
        return {r["id"]: r["type"] for r in rows}

    async def get_entity(self, entity_id: str, t: datetime) -> EntityView:
        rows = await self._read(
            "MATCH (e:Entity {entity_id: $id}) "
            "OPTIONAL MATCH (e)-[im:IS_MODEL]->(m:HwModel) "
            "  WHERE im.valid_from <= $t AND $t < coalesce(im.valid_to, " + END_OF_TIME + ") "
            "OPTIONAL MATCH (m)-[:OF_GENERATION]->(g:Generation) "
            "OPTIONAL MATCH (snap:RunningSnapshot)-[:SNAPSHOT_OF]->(e) "
            "  WHERE snap.valid_from <= $t AND $t < coalesce(snap.valid_to, " + END_OF_TIME + ") "
            "WITH e, m, g, snap "
            "OPTIONAL MATCH (s:Segment)-[c:CONNECTS]->(e) "
            "  WHERE c.valid_from <= $t AND $t < coalesce(c.valid_to, " + END_OF_TIME + ") "
            "WITH e, m, g, snap, collect(DISTINCT s { .entity_id, .name, .private_road, "
            "  .barrier }) AS segments "
            "OPTIONAL MATCH (e)-[l:LINKED]-(o:Device) "
            "  WHERE l.valid_from <= $t AND $t < coalesce(l.valid_to, " + END_OF_TIME + ") "
            "WITH e, m, g, snap, segments, collect(DISTINCT o.entity_id) AS linked "
            "OPTIONAL MATCH (fw:DeviceEvent {type: 'firmware_upgrade'})-[:ON]->(e) "
            "WITH e, m, g, snap, segments, linked, fw ORDER BY fw.event_time "
            "RETURN e { .*, embedding: null } AS e, m.entity_id AS model, "
            "g.entity_id AS generation, snap.hw_model AS running_hw_model, segments, linked, "
            "collect(fw { .event_time, .payload_json }) AS upgrades",
            id=entity_id,
            t=t,
        )
        if not rows:
            raise NotFoundError(f"unknown entity {entity_id!r}")
        r = py(rows[0])
        e = r["e"]
        firmware, source = _firmware_at(e, r["upgrades"], t)
        exists = (
            e.get("valid_from") is not None
            and e["valid_from"] <= t
            and (e.get("valid_to") is None or t < e["valid_to"])
        )
        keep = {"vendor", "kind", "serial", "rack", "commissioned", "segment_type", "role"}
        members: list[str] = []
        if e.get("entity_type") in ("hw_model", "generation"):
            members = await self.members(entity_id, t)
        return EntityView(
            entity_id=entity_id,
            entity_type=e.get("entity_type") or "unknown",
            name=e.get("name"),
            as_of=t,
            exists_at=exists,
            valid_from=e.get("valid_from"),
            valid_to=e.get("valid_to"),
            model=r["model"] or (e.get("model") if e.get("entity_type") == "device" else None),
            generation=r["generation"] or e.get("generation"),
            firmware=firmware,
            firmware_source=source,
            running_hw_model=r["running_hw_model"],
            status=("in_service" if exists else "not_in_service")
            if e.get("entity_type") == "device"
            else None,
            location=e.get("room") or e.get("city"),
            segments=[SegmentRef.model_validate(s) for s in r["segments"] if s.get("entity_id")],
            linked_devices=sorted(x for x in r["linked"] if x),
            members=members,
            properties={k: v for k, v in e.items() if k in keep and v is not None},
        )

    async def members(self, model_or_generation: str, t: datetime) -> list[str]:
        """Devices in service at t whose model (as built at t) is this model or of this
        generation."""
        rows = await self._read(
            "MATCH (d:Device)-[im:IS_MODEL]->(m:HwModel) "
            "WHERE (m.entity_id = $id OR (m)-[:OF_GENERATION]->(:Generation {entity_id: $id})) "
            "  AND im.valid_from <= $t AND $t < coalesce(im.valid_to, " + END_OF_TIME + ") "
            "  AND d.valid_from <= $t AND $t < coalesce(d.valid_to, " + END_OF_TIME + ") "
            "RETURN DISTINCT d.entity_id AS id ORDER BY id",
            id=model_or_generation,
            t=t,
        )
        return [row["id"] for row in rows]

    async def scope(self, entity_id: str, t: datetime) -> dict[str, Match]:
        """The entity, its model and generation, its site, and the segments connecting it,
        at t (R9: node -> HwModel -> Generation -> Site -> Segment)."""
        rows = await self._read(
            "MATCH (e:Entity {entity_id: $id}) "
            "OPTIONAL MATCH (e)-[im:IS_MODEL]->(m:HwModel) "
            "  WHERE im.valid_from <= $t AND $t < coalesce(im.valid_to, " + END_OF_TIME + ") "
            "OPTIONAL MATCH (m)-[:OF_GENERATION]->(g:Generation) "
            "OPTIONAL MATCH (e)-[:OF_GENERATION]->(own:Generation) "
            "OPTIONAL MATCH (e)-[at:AT_SITE]->(site:Site) "
            "  WHERE at.valid_from <= $t AND $t < coalesce(at.valid_to, " + END_OF_TIME + ") "
            "OPTIONAL MATCH (s:Segment)-[c:CONNECTS]->(e) "
            "  WHERE c.valid_from <= $t AND $t < coalesce(c.valid_to, " + END_OF_TIME + ") "
            "RETURN e.entity_id AS e, m.entity_id AS m, "
            "coalesce(g.entity_id, own.entity_id) AS g, "
            "collect(DISTINCT site.entity_id) AS sites, "
            "collect(DISTINCT s.entity_id) AS segments",
            id=entity_id,
            t=t,
        )
        if not rows:
            raise NotFoundError(f"unknown entity {entity_id!r}")
        r = rows[0]
        scope: dict[str, Match] = {r["e"]: Match.DIRECT}
        for key in ("m", "g"):
            if r[key]:
                scope.setdefault(r[key], Match.MODEL)
        for site in r["sites"]:
            scope.setdefault(site, Match.SITE)
        for seg in r["segments"]:
            scope.setdefault(seg, Match.SEGMENT)
        return scope

    # --- Knowledge ------------------------------------------------------------------------

    async def context(
        self,
        scope: Mapping[str, Match],
        t: datetime,
        k: datetime,
        *,
        same_model_of: str | None = None,
    ) -> list[AssertionView]:
        """Assertions in scope at t and known at k, bound to any scope entity; plus (when
        `same_model_of` names a model) assertions observed on other devices of that model."""
        rows = await self._read(
            "UNWIND $scope AS s "
            "MATCH (a:ContextAssertion)-[r:ABOUT]->(:Entity {entity_id: s.id}) "
            "WHERE " + _CONFIRMED + " AND " + _VALID + " AND " + _KNOWN + " "
            "WITH a, min(s.rank) AS rank "
            "RETURN rank, " + _ASSERTION_FIELDS,
            scope=[{"id": i, "rank": _RANK[m]} for i, m in scope.items()],
            t=t,
            k=k,
        )
        views: dict[str, AssertionView] = {
            r["a"]["assertion_id"]: assertion_view(r, _MATCH_BY_RANK[r["rank"]], k) for r in rows
        }
        if same_model_of:
            others = await self._read(
                "MATCH (a:ContextAssertion)-[r:ABOUT {scope: 'observed_on'}]->(o:Device)"
                "-[im:IS_MODEL]->(:HwModel {entity_id: $model}) "
                "WHERE NOT o.entity_id IN $ids AND "
                + _CONFIRMED
                + " AND "
                + _VALID
                + " AND "
                + _KNOWN
                + " "
                "AND im.valid_from <= $t AND $t < coalesce(im.valid_to, " + END_OF_TIME + ") "
                "WITH DISTINCT a RETURN " + _ASSERTION_FIELDS,
                model=same_model_of,
                ids=list(scope),
                t=t,
                k=k,
            )
            for r in others:
                views.setdefault(
                    r["a"]["assertion_id"], assertion_view(r, Match.SAME_MODEL_DEVICE, k)
                )
        return sorted(views.values(), key=lambda v: (-(v.score or 0), v.assertion_id))

    async def assertions(
        self, ids: Sequence[str], t: datetime | None = None, k: datetime | None = None
    ) -> list[AssertionView]:
        """By id; with t/k, only those in scope at t and known at k."""
        if t is not None and k is not None:
            rows = await self._read(
                "MATCH (a:ContextAssertion) WHERE a.assertion_id IN $ids AND "
                + _VALID
                + " AND "
                + _KNOWN
                + " RETURN "
                + _ASSERTION_FIELDS,
                ids=list(ids),
                t=t,
                k=k,
            )
        else:
            rows = await self._read(
                "MATCH (a:ContextAssertion) WHERE a.assertion_id IN $ids RETURN "
                + _ASSERTION_FIELDS,
                ids=list(ids),
            )
        by_id = {r["a"]["assertion_id"]: assertion_view(r, None, k) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    async def valid_assertion_ids(self, t: datetime, k: datetime) -> list[str]:
        """Every assertion in force at t and on record at k (R9/R10), whatever its status
        today: the candidate set that semantic search ranks (R12, plan 004)."""
        rows = await self._read(
            "MATCH (a:ContextAssertion) WHERE " + _VALID + " AND " + _KNOWN + " "
            "RETURN a.assertion_id AS id ORDER BY id",
            t=t,
            k=k,
        )
        return [str(r["id"]) for r in rows]

    async def keyword_search(
        self, terms: Sequence[str], t: datetime, k: datetime, limit: int
    ) -> list[AssertionView]:
        """Fallback when embeddings are unavailable: assertions whose statement or topic
        contains the most query terms."""
        rows = await self._read(
            "MATCH (a:ContextAssertion) WHERE " + _VALID + " AND " + _KNOWN + " "
            "WITH a, size([w IN $terms WHERE toLower(a.statement) CONTAINS w "
            "  OR toLower(coalesce(a.topic, '')) CONTAINS w]) AS hits WHERE hits > 0 "
            "WITH a, hits ORDER BY hits DESC LIMIT $limit "
            "RETURN " + _ASSERTION_FIELDS,
            terms=[w.lower() for w in terms],
            t=t,
            k=k,
            limit=limit,
        )
        return [assertion_view(r, None, k) for r in rows]

    async def search_assertions(self, vec: Sequence[float], k: int) -> list[vector.VectorHit]:
        """Global semantic top-k over assertions that are not superseded or expired."""
        return await vector.search(
            self._driver,
            self._db,
            vector.ASSERTIONS,
            vec,
            k,
            status_in=["believed_true", "unverified", "disputed"],
        )

    async def rank_assertions(
        self, ids: Sequence[str], vec: Sequence[float], k: int
    ) -> list[vector.VectorHit]:
        """Exact ranking of an already-scoped candidate set (spike BE-1.2)."""
        return await vector.rank_candidates(self._driver, self._db, vector.ASSERTIONS, ids, vec, k)

    async def assertions_missing_embeddings(self, limit: int = 500) -> list[dict[str, str]]:
        return await self._read(
            "MATCH (a:ContextAssertion) WHERE a.embedding IS NULL AND a.statement IS NOT NULL "
            "RETURN a.assertion_id AS id, a.statement AS statement LIMIT $limit",
            limit=limit,
        )

    async def set_embeddings(self, rows: Sequence[Mapping[str, Any]]) -> None:
        await self._driver.execute_query(
            "UNWIND $rows AS r MATCH (a:ContextAssertion {assertion_id: r.id}) "
            "SET a.embedding = r.vector",
            rows=list(rows),
            database_=self._db,
        )

    async def site_access(
        self, scope: Mapping[str, Match], t: datetime, k: datetime
    ) -> tuple[list[SegmentRef], list[AssertionView], list[AccessRecordView]]:
        seg_ids = [i for i, m in scope.items() if m in (Match.SEGMENT, Match.DIRECT)]
        segs = await self._read(
            "MATCH (s:Segment) WHERE s.entity_id IN $ids "
            "RETURN s { .entity_id, .name, .private_road, .barrier } AS s",
            ids=seg_ids,
        )
        rows = await self._read(
            "UNWIND $ids AS id "
            "MATCH (a:ContextAssertion)-[r:ABOUT]->(:Entity {entity_id: id}) "
            "WHERE " + _CONFIRMED + " AND (a.topic CONTAINS 'access' OR a.topic CONTAINS 'site') "
            "AND " + _VALID + " AND " + _KNOWN + " "
            "WITH DISTINCT a RETURN " + _ASSERTION_FIELDS,
            ids=list(scope),
            t=t,
            k=k,
        )
        register = await self._read(
            "MATCH (r:AccessRecord)-[:FOR]->(s:Segment) WHERE s.entity_id IN $ids "
            "AND r.recorded_at <= $k "
            # As known at k: the next register row closes this one only once recorded.
            "WITH r, s, CASE WHEN EXISTS { MATCH (n:AccessRecord)-[:FOR]->(s) "
            "  WHERE n.valid_from = r.valid_to AND n.recorded_at > $k } "
            "  THEN null ELSE r.valid_to END AS valid_to "
            "WHERE r.valid_from <= $t AND $t < coalesce(valid_to, " + END_OF_TIME + ") "
            "RETURN s.entity_id AS segment_id, r { .*, valid_to: valid_to } AS r",
            ids=seg_ids,
            t=t,
            k=k,
        )
        return (
            [SegmentRef.model_validate(py(r["s"])) for r in segs],
            [assertion_view(r, Match.SEGMENT, k) for r in rows],
            [
                AccessRecordView.model_validate({**py(r["r"]), "segment_id": r["segment_id"]})
                for r in register
            ],
        )

    async def events(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        kinds: Sequence[str] | None,
        limit: int,
    ) -> tuple[list[EventView], bool]:
        rows = await self._read(
            "MATCH (ev:Event)-[:ON]->(:Entity {entity_id: $id}) "
            "WHERE ev.event_time >= $start AND ev.event_time < $end "
            "WITH ev, [l IN labels(ev) WHERE l <> 'Event'][0] AS kind "
            "WHERE $kinds IS NULL OR kind IN $kinds OR ev.type IN $kinds "
            "RETURN ev { .* } AS ev, kind ORDER BY ev.event_time, ev.event_id LIMIT $limit",
            id=entity_id,
            start=start,
            end=end,
            kinds=list(kinds) if kinds else None,
            limit=limit + 1,
        )
        out = []
        for r in rows[:limit]:
            ev = py(r["ev"])
            details = {
                k: v
                for k, v in ev.items()
                if k not in {"event_id", "event_time", "type", "severity", "text", "origin"}
                and v is not None
            }
            out.append(
                EventView(
                    event_id=ev["event_id"],
                    kind=r["kind"] or "Event",
                    event_time=ev["event_time"],
                    type=ev.get("type"),
                    severity=ev.get("severity"),
                    text=ev.get("text") or ev.get("description"),
                    entity_id=entity_id,
                    details=details,
                )
            )
        return out, len(rows) > limit

    async def history(
        self, assertion_id: str
    ) -> tuple[
        list[AssertionView],
        list[AssertionView],
        list[AssertionView],
        list[EvidenceView],
        list[FlagView],
    ]:
        rows = await self._read(
            "MATCH (a:ContextAssertion {assertion_id: $id}) "
            "OPTIONAL MATCH chain = (a)-[:SUPERSEDES*0..20]-(v:ContextAssertion) "
            "WITH a, collect(DISTINCT v.assertion_id) AS versions "
            "OPTIONAL MATCH (a)-[:CONTRADICTS]-(c:ContextAssertion) "
            "WITH a, versions, collect(DISTINCT c.assertion_id) AS contradicts "
            "OPTIONAL MATCH (a)-[:CORROBORATES]-(o:ContextAssertion) "
            "WITH a, versions, contradicts, collect(DISTINCT o.assertion_id) AS corroborates "
            "OPTIONAL MATCH (ev:Event)-[r:SUPPORTS|REFUTES]->(a) "
            "OPTIONAL MATCH (ev)-[:ON]->(x:Entity) "
            "WITH a, versions, contradicts, corroborates, "
            "  collect(DISTINCT CASE WHEN ev IS NULL THEN null ELSE {event_id: ev.event_id, "
            "  relation: type(r), event_time: ev.event_time, result: ev.result, "
            "  entity_id: x.entity_id, agent_id: ev.agent_id, "
            "  conditions_met: r.conditions_met} END) AS evidence "
            "OPTIONAL MATCH (f:Flag)-[:FLAGS]->(a) "
            "RETURN versions, contradicts, corroborates, evidence, "
            "collect(f { .flag_id, .reason, .agent_id, .flagged_at }) AS flags",
            id=assertion_id,
        )
        if not rows:
            raise NotFoundError(f"unknown assertion {assertion_id!r}")
        r = py(rows[0])
        versions = sorted(await self.assertions(r["versions"]), key=lambda v: v.valid_from)
        return (
            versions,
            await self.assertions(r["contradicts"]),
            await self.assertions(r["corroborates"]),
            sorted(
                (EvidenceView.model_validate(e) for e in r["evidence"] if e),
                key=lambda e: e.event_time,
            ),
            [FlagView.model_validate(f) for f in r["flags"] if f.get("flag_id")],
        )

    async def top_author(self, entity_ids: Sequence[str]) -> str | None:
        """The person who has written most of the knowledge about these entities."""
        rows = await self._read(
            "MATCH (a:ContextAssertion)-[:ABOUT]->(e:Entity) WHERE e.entity_id IN $ids "
            "AND a.asserted_by IS NOT NULL "
            "RETURN a.asserted_by AS who, count(DISTINCT a) AS n ORDER BY n DESC, who LIMIT 1",
            ids=list(entity_ids),
        )
        return rows[0]["who"] if rows else None

    async def existing_events(self, ids: Sequence[str]) -> set[str]:
        rows = await self._read(
            "MATCH (e:Event) WHERE e.event_id IN $ids RETURN e.event_id AS id", ids=list(ids)
        )
        return {r["id"] for r in rows}

    # --- The single write path for knowledge (BE-1.5; plan 001 WS1, R2-R7) -----------------
    # Each public method is one transaction: the write, then the V5 checks on every assertion
    # it touched. A violation raises WriteRejectedError (422) and the transaction rolls back.

    async def record_tool_call(self, call: Mapping[str, Any]) -> None:
        """One MCP tool call, for the console's agent runs (UI-1). Operational, like
        Thread: never read by temporal queries."""
        await self._driver.execute_query(
            "CREATE (c:ToolCall) SET c = $call", {"call": dict(call)}, database_=self._db
        )

    async def _tx[T](self, work: Callable[[AsyncManagedTransaction], Awaitable[T]]) -> T:
        async with self._driver.session(database=self._db) as session:
            try:
                return await session.execute_write(work)
            except ConstraintError:
                # The same idempotency key won a concurrent race: the retry replays it.
                return await session.execute_write(work)

    async def assert_(self, item: AssertionIn) -> AssertionOut:
        """Create an assertion with its ABOUT bindings, DERIVED_FROM sources (and their
        AUTHORED_BY authors), and CONTRADICTS / CORROBORATES links. Idempotent on the
        completing source event + statement."""

        async def work(tx: AsyncManagedTransaction) -> AssertionOut:
            out = await _create(tx, item)
            if not out.replayed:
                await _check_v5(tx, [out.assertion_id, *item.contradicts, *item.corroborates])
            return out

        return await self._tx(work)

    async def supersede(self, predecessor_id: str, successor: AssertionIn) -> AssertionOut:
        """R4b / R6(i), a change in the world: the successor starts, and the predecessor's
        valid_to becomes the successor's valid_from ("superseded timestamp")."""

        async def work(tx: AsyncManagedTransaction) -> AssertionOut:
            pred = await _state(tx, predecessor_id)
            if pred.retracted_at is not None:
                raise WriteRejectedError(f"{predecessor_id} is retracted; nothing to supersede")
            if await _single(
                tx,
                "MATCH (s:ContextAssertion)-[:SUPERSEDES]->(:ContextAssertion {assertion_id: $id}) "
                "WHERE s.retracted_at IS NULL RETURN true AS x",
                id=predecessor_id,
            ):
                raise WriteRejectedError(f"{predecessor_id} is already superseded")
            out = await _create(tx, successor)
            if out.replayed:
                return out
            ended = _rule(
                rules.supersede,
                pred,
                rules.TemporalState(valid_from=out.valid_from, recorded_at=out.recorded_at),
            )
            await tx.run(
                "MATCH (p:ContextAssertion {assertion_id: $pred}), "
                "(s:ContextAssertion {assertion_id: $succ}) "
                "SET p.status_before = CASE WHEN p.status = 'superseded' "
                "  THEN p.status_before ELSE p.status END, "
                "  p.valid_to = $valid_to, p.status = $status, p.superseded_by = $succ "
                "CREATE (s)-[:SUPERSEDES]->(p)",
                pred=predecessor_id,
                succ=out.assertion_id,
                valid_to=ended.valid_to,
                status=ended.status.value,
            )
            await _check_v5(tx, [predecessor_id, out.assertion_id])
            return out

        return await self._tx(work)

    async def correct(
        self, wrong_id: str, fixed: AssertionIn, correction_event_id: str
    ) -> AssertionOut:
        """R6(ii), a person fixing an extraction mistake. The wrong version is retracted at the
        correction's event_time; the fixed version keeps its validity window (not a change in
        the world, so no SUPERSEDES) and is recorded at the correction."""

        async def work(tx: AsyncManagedTransaction) -> AssertionOut:
            wrong = await _state(tx, wrong_id)
            at = await _single(
                tx,
                "MATCH (e:Event {event_id: $id}) RETURN e.event_time AS t",
                id=correction_event_id,
            )
            if at is None:
                raise WriteRejectedError(f"unknown correction event {correction_event_id!r}")
            _, fixed_state = _rule(rules.correct, wrong, py(at))
            sources = list(fixed.sources)
            if correction_event_id not in {src.event_id for src in sources}:
                sources.append(SourceIn(event_id=correction_event_id))
            item = fixed.model_copy(
                update={
                    "valid_from": fixed_state.valid_from,
                    "valid_to": fixed_state.valid_to,
                    "sources": sources,
                }
            )
            out = await _create(tx, item)
            if out.replayed:
                return out
            if out.recorded_at != fixed_state.recorded_at:
                raise WriteRejectedError("a source of the fix is later than the correction (R2)")
            preds = await _single(
                tx,
                "MATCH (w:ContextAssertion {assertion_id: $wrong}), "
                "(f:ContextAssertion {assertion_id: $fixed}) "
                "SET w.retracted_at = $at CREATE (f)-[:CORRECTS]->(w) "
                # The fix takes the wrong version's place in the supersession chain. The
                # wrong version keeps its edges: what was believed then stays on record (R6).
                "WITH w, f "
                "CALL (w, f) { MATCH (s:ContextAssertion)-[:SUPERSEDES]->(w) "
                "  WHERE s.retracted_at IS NULL "
                "  CREATE (s)-[:SUPERSEDES]->(f) "
                "  SET f.status_before = f.status, f.status = 'superseded', "
                "    f.superseded_by = s.assertion_id } "
                "CALL (w, f) { MATCH (w)-[:SUPERSEDES]->(p:ContextAssertion) "
                "  CREATE (f)-[:SUPERSEDES]->(p) SET p.superseded_by = f.assertion_id } "
                # A fix corrects the wording or scope, not a disagreement with other
                # knowledge: it inherits the live CORROBORATES / CONTRADICTS links, and
                # stays disputed unless it outranks the other side (V6).
                "CALL (w, f) { MATCH (w)-[:CORROBORATES]-(x:ContextAssertion) "
                "  WHERE x.retracted_at IS NULL AND x <> f MERGE (f)-[:CORROBORATES]->(x) } "
                "CALL (w, f) { MATCH (w)-[:CONTRADICTS]-(x:ContextAssertion) "
                "  WHERE x.retracted_at IS NULL AND x <> f MERGE (f)-[:CONTRADICTS]->(x) "
                "  WITH f, max(coalesce(x.authority, 1)) AS top "
                "  SET f.status = CASE WHEN f.status IN ['superseded', 'expired'] THEN f.status "
                "    WHEN coalesce(f.authority, 1) > top THEN f.status ELSE 'disputed' END } "
                "RETURN [(f)-[:SUPERSEDES]->(p:ContextAssertion) | p.assertion_id] AS preds",
                wrong=wrong_id,
                fixed=out.assertion_id,
                at=fixed_state.recorded_at,
            )
            await _check_v5(tx, [wrong_id, out.assertion_id, *preds])
            return await _out(tx, out.assertion_id, replayed=False)

        return await self._tx(work)

    async def retract(self, assertion_id: str, at: datetime) -> AssertionOut:
        """Withdraw an assertion from what is known from `at` on (R6: nothing is deleted).
        Retracting again at the same instant is a no-op."""

        async def work(tx: AsyncManagedTransaction) -> AssertionOut:
            state = await _state(tx, assertion_id)
            at_utc = rules.ensure_utc(at)
            if state.retracted_at is not None:
                if state.retracted_at != at_utc:
                    raise WriteRejectedError(f"{assertion_id} is already retracted")
                replayed = True
            else:
                _rule(state.replace, retracted_at=at_utc)
                await tx.run(
                    "MATCH (a:ContextAssertion {assertion_id: $id}) SET a.retracted_at = $at",
                    id=assertion_id,
                    at=at_utc,
                )
                # BE-1.8: what it superseded, with no other live successor, is believed
                # again. Its old valid_to and the SUPERSEDES edge stay on record (R6, R10).
                reopened = await _single(
                    tx,
                    "OPTIONAL MATCH (:ContextAssertion {assertion_id: $id})-[:SUPERSEDES]->"
                    "(p:ContextAssertion) WHERE p.retracted_at IS NULL AND NOT EXISTS { "
                    "  MATCH (x:ContextAssertion)-[:SUPERSEDES]->(p) "
                    "  WHERE x.retracted_at IS NULL } "
                    "SET p.status = coalesce(p.status_before, 'believed_true'), "
                    "  p.superseded_by = null "
                    "RETURN collect(p.assertion_id) AS ids",
                    id=assertion_id,
                )
                await _check_v5(tx, [assertion_id, *(reopened or [])])
                replayed = False
            return await _out(tx, assertion_id, replayed=replayed)

        return await self._tx(work)

    async def add_evidence(
        self,
        outcome_event_id: str,
        assertion_id: str,
        relation: Literal["SUPPORTS", "REFUTES"],
        conditions_met: bool | None = None,
        review_due: bool = False,
    ) -> dict[str, Any]:
        """Link an Outcome to an assertion as evidence and update support_n / refute_n.
        Linking the same outcome twice counts once."""

        async def work(tx: AsyncManagedTransaction) -> dict[str, Any]:
            try:
                counters = await _evidence(
                    tx, outcome_event_id, assertion_id, relation, conditions_met, review_due
                )
            except NotFoundError as exc:
                raise WriteRejectedError(str(exc)) from exc
            await _check_v5(tx, [assertion_id])
            return counters

        return await self._tx(work)

    async def confirm(self, assertion_id: str) -> AssertionOut:
        """A person's ✅ on the Slack card: unverified -> believed_true. Disputed, superseded
        and retracted versions keep their status (the click is still an event upstream)."""

        async def work(tx: AsyncManagedTransaction) -> AssertionOut:
            state = await _state(tx, assertion_id)
            if state.retracted_at is None and state.status is AssertionStatus.UNVERIFIED:
                await tx.run(
                    "MATCH (a:ContextAssertion {assertion_id: $id}) SET a.status = $s",
                    id=assertion_id,
                    s=AssertionStatus.BELIEVED_TRUE.value,
                )
                await _check_v5(tx, [assertion_id])
            return await _out(tx, assertion_id, replayed=False)

        return await self._tx(work)

    async def bind_candidate(self, assertion_id: str, entity_id: str) -> None:
        """The answer to the clarifying question (V3): the chosen candidate becomes a binding,
        the other candidates of the same scope are dropped."""

        async def work(tx: AsyncManagedTransaction) -> None:
            row = await _single(
                tx,
                "MATCH (a:ContextAssertion {assertion_id: $id})-[b:ABOUT {candidate: true}]->"
                "(:Entity {entity_id: $entity}) SET b.candidate = false RETURN b.scope AS s",
                id=assertion_id,
                entity=entity_id,
            )
            if row is None:
                raise WriteRejectedError(f"{entity_id} is not a candidate of {assertion_id}")
            await tx.run(
                "MATCH (:ContextAssertion {assertion_id: $id})-[b:ABOUT {candidate: true}]->() "
                "WHERE b.scope = $scope DELETE b",
                id=assertion_id,
                scope=row,
            )
            await _check_v5(tx, [assertion_id])

        await self._tx(work)

    async def sources(self, assertion_id: str) -> list[SourceIn]:
        """The events an assertion derives from, with their quotes (for a correction)."""
        rows = await self._read(
            "MATCH (:ContextAssertion {assertion_id: $id})-[d:DERIVED_FROM]->(e:Event) "
            "RETURN e.event_id AS event_id, d.quote AS quote ORDER BY e.event_time",
            id=assertion_id,
        )
        return [SourceIn.model_validate(r) for r in rows]

    async def network_check(
        self,
        *,
        assertion_id: str,
        entity_id: str,
        port: str | None,
        observed_at: datetime,
        agrees: bool,
        detail: str,
    ) -> None:
        """V4: the telemetry check of the symptoms claimed in the thread, as an Outcome event
        from the network. Agreement adds one SUPPORTS; disagreement is recorded, not counted."""
        event_id = f"NET-{assertion_id}-{entity_id}"

        async def work(tx: AsyncManagedTransaction) -> None:
            if await _single(tx, "MATCH (o:Outcome {event_id: $id}) RETURN true", id=event_id):
                return
            await tx.run(
                "MATCH (d:Entity {entity_id: $entity}) "
                "CREATE (o:Event:Outcome {event_id: $id, event_time: $t, platform: 'network', "
                "  action: 'telemetry_check', result: $result, port: $port, detail: $detail, "
                "  agent_id: 'network'}) CREATE (o)-[:ON]->(d)",
                entity=entity_id,
                id=event_id,
                t=rules.ensure_utc(observed_at),
                result="agrees" if agrees else "disagrees",
                port=port,
                detail=detail,
            )
            if agrees:
                await _evidence(tx, event_id, assertion_id, "SUPPORTS", True, False)
            await _check_v5(tx, [assertion_id])

        await self._tx(work)

    async def reconcile_candidates(
        self,
        topic: str | None,
        entity_ids: Sequence[str],
        t: datetime,
        extra_ids: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """V6 candidates: live assertions on the same topic, bound to one of the entities or
        found by vector search (`extra_ids`), valid at t, with where they came from."""
        rows: list[dict[str, Any]] = py(
            await self._read(
                "MATCH (a:ContextAssertion) WHERE a.retracted_at IS NULL "
                "AND a.valid_from <= $t AND $t < coalesce(a.valid_to, " + END_OF_TIME + ") "
                "AND ((a.topic IS NOT NULL AND a.topic = $topic) OR a.assertion_id IN $extra "
                "  OR EXISTS { MATCH (a)-[:ABOUT]->(e:Entity) WHERE e.entity_id IN $ids }) "
                "RETURN a.assertion_id AS assertion_id, a.statement AS statement, "
                "a.topic AS topic, a.status AS status, a.recorded_at AS recorded_at, "
                "a.valid_from AS valid_from, a.asserted_by AS author, "
                "coalesce(a.authority, 1) AS authority, a.qualifiers_json AS qualifiers_json, "
                "[(a)-[:DERIVED_FROM]->(ev:Event) | ev.thread_id] AS threads, "
                "head([(a)-[:DERIVED_FROM]->(ev:Event) | ev.author_role]) AS role, "
                "head([(a)-[:DERIVED_FROM]->(ev:Event) | ev.platform]) AS platform, "
                "[(a)-[b:ABOUT]->(e:Entity) WHERE NOT coalesce(b.candidate, false) "
                "  AND b.scope <> 'activity' | {entity_id: e.entity_id, scope: b.scope}] AS about "
                "ORDER BY a.recorded_at DESC LIMIT 20",
                topic=topic,
                ids=list(entity_ids),
                extra=list(extra_ids),
                t=t,
            )
        )
        return rows

    # --- Writes ---------------------------------------------------------------------------

    async def write_once(
        self,
        *,
        agent_id: str,
        tool: str,
        idempotency_key: str,
        now: datetime,
        work: Any,
        on_behalf_of: str | None = None,
        trigger_event_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Run `work(tx) -> dict` once per (agent, tool, key). Returns (result, replayed)."""
        key = f"{agent_id}:{tool}:{idempotency_key}"

        async def tx_fn(tx: AsyncManagedTransaction) -> tuple[dict[str, Any], bool]:
            found = await (
                await tx.run("MATCH (r:WriteReceipt {key: $key}) RETURN r.result AS r", key=key)
            ).single()
            if found is not None:
                return json.loads(found["r"]), True
            result: dict[str, Any] = await work(tx)
            if result.get("assertion_id"):  # V5 on the assertion this write touched
                await _check_v5(tx, [result["assertion_id"]])
            action_id = new_id("ACT")
            await tx.run(
                "CREATE (r:WriteReceipt {key: $key, result: $result, created_at: $now}) "
                "CREATE (e:Event:AgentAction {event_id: $action, event_time: $now, "
                "  platform: 'mcp', tool: $tool, agent_id: $agent, on_behalf_of: $behalf, "
                "  idempotency_key: $ikey, trigger_event_id: $trigger, ingested_at: $now}) "
                "CREATE (e)-[:RECEIPT]->(r) "
                # Link the event that prompted the write when it is on record (a sim alarm
                # may reach the agent before the graph).
                "WITH e OPTIONAL MATCH (t:Event {event_id: $trigger}) "
                "FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | "
                "  CREATE (e)-[:TRIGGERED_BY]->(t))",
                key=key,
                result=json.dumps(result, default=_json_default),
                now=now,
                action=action_id,
                tool=tool,
                agent=agent_id,
                behalf=on_behalf_of,
                ikey=idempotency_key,
                trigger=trigger_event_id,
            )
            return result, False

        async with self._driver.session(database=self._db) as session:
            try:
                return await session.execute_write(tx_fn)
            except ConstraintError:
                # A concurrent call with the same key won the race: return its result.
                return await session.execute_write(tx_fn)

    async def record_outcome(
        self,
        *,
        agent_id: str,
        idempotency_key: str,
        now: datetime,
        entity_id: str,
        assertion_id: str | None,
        port: str | None,
        action: str,
        result: str,
        observed_at: datetime,
        evidence_ref: str | None,
        conditions_met: bool | None,
        on_behalf_of: str | None = None,
        trigger_event_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """An Outcome event (plan 001 §6.2). Evidence for the assertion:
        resolved -> SUPPORTS (support_n + 1);
        not_resolved with conditions met -> REFUTES (refute_n + 1, review_due);
        not_resolved with conditions not met -> not counted (the rule didn't apply);
        not_resolved with unknown conditions -> not counted, but review_due."""
        event_id = new_id("OUT")
        relation: str | None = None
        if assertion_id and result == "resolved":
            relation = "SUPPORTS"
        elif assertion_id and result == "not_resolved" and conditions_met is True:
            relation = "REFUTES"
        review = bool(assertion_id and result == "not_resolved" and conditions_met is not False)

        async def work(tx: AsyncManagedTransaction) -> dict[str, Any]:
            await tx.run(
                "MATCH (d:Entity {entity_id: $entity}) "
                "CREATE (o:Event:Outcome {event_id: $id, event_time: $observed, "
                "  ingested_at: $now, platform: 'mcp', action: $action, result: $result, "
                "  port: $port, evidence_ref: $ref, agent_id: $agent, "
                "  on_behalf_of: $behalf, conditions_met: $met}) "
                "CREATE (o)-[:ON]->(d)",
                entity=entity_id,
                id=event_id,
                observed=observed_at,
                now=now,
                action=action,
                result=result,
                port=port,
                ref=evidence_ref,
                agent=agent_id,
                behalf=on_behalf_of,
                met=conditions_met,
            )
            out: dict[str, Any] = {
                "event_id": event_id,
                "assertion_id": assertion_id,
                "evidence": relation,
                "conditions_met": conditions_met,
            }
            if not assertion_id:
                return out
            counters = await _evidence(tx, event_id, assertion_id, relation, conditions_met, review)
            out |= {
                "support_n": counters["support_n"],
                "refute_n": counters["refute_n"],
                "review_due": counters["review"],
            }
            return out

        return await self.write_once(
            agent_id=agent_id,
            tool="report_outcome",
            idempotency_key=idempotency_key,
            now=now,
            work=work,
            on_behalf_of=on_behalf_of,
            trigger_event_id=trigger_event_id,
        )

    async def open_gap(
        self,
        *,
        agent_id: str,
        idempotency_key: str,
        now: datetime,
        question: str,
        entity_ids: Sequence[str],
        context: str | None,
        routed_to: str | None,
        on_behalf_of: str | None = None,
        trigger_event_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        gap_id = new_id("GAP")

        async def work(tx: AsyncManagedTransaction) -> dict[str, Any]:
            await tx.run(
                "CREATE (g:KnowledgeGap {gap_id: $id, question: $q, context: $ctx, "
                "  status: 'open', asked_at: $now, asked_by: $agent, routed_to: $to}) "
                "WITH g UNWIND $ids AS eid MATCH (e:Entity {entity_id: eid}) "
                "CREATE (g)-[:ABOUT]->(e)",
                id=gap_id,
                q=question,
                ctx=context,
                now=now,
                agent=agent_id,
                to=routed_to,
                ids=list(entity_ids),
            )
            return {
                "gap_id": gap_id,
                "question": question,
                "entity_ids": list(entity_ids),
                "routed_to": routed_to,
            }

        return await self.write_once(
            agent_id=agent_id,
            tool="open_knowledge_gap",
            idempotency_key=idempotency_key,
            now=now,
            work=work,
            on_behalf_of=on_behalf_of,
            trigger_event_id=trigger_event_id,
        )

    async def propose(
        self,
        *,
        agent_id: str,
        idempotency_key: str,
        now: datetime,
        statement: str,
        topic: str | None,
        bindings: Sequence[Mapping[str, str]],
        qualifiers: Mapping[str, Any],
        valid_from: datetime,
        valid_to: datetime | None,
        evidence_event_ids: Sequence[str],
        rationale: str,
        on_behalf_of: str | None = None,
        trigger_event_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """An agent proposal: origin agent_proposal, status unverified, recorded now (R2:
        the proposal is itself the recording event), derived from existing events."""
        assertion_id = new_id("ASR-P")

        async def work(tx: AsyncManagedTransaction) -> dict[str, Any]:
            await tx.run(
                "CREATE (a:ContextAssertion {assertion_id: $id, origin: 'agent_proposal', "
                "  status: 'unverified', statement: $statement, topic: $topic, "
                "  qualifiers_json: $qualifiers, valid_from: $vf, valid_to: $vt, "
                "  recorded_at: $now, proposed_by: $agent, rationale: $rationale, "
                "  support_n: 0, refute_n: 0, review_due: false, confidence: 0.5, "
                "  source_type: 'agent_proposal', source_ref: $agent}) "
                "WITH a UNWIND $bindings AS b MATCH (e:Entity {entity_id: b.entity_id}) "
                "CREATE (a)-[:ABOUT {scope: b.scope, resolution_confidence: 1.0}]->(e)",
                id=assertion_id,
                statement=statement,
                topic=topic,
                qualifiers=json.dumps(dict(qualifiers), sort_keys=True),
                vf=valid_from,
                vt=valid_to,
                now=now,
                agent=agent_id,
                rationale=rationale,
                bindings=list(bindings),
            )
            await tx.run(
                "MATCH (a:ContextAssertion {assertion_id: $id}) "
                "UNWIND $events AS eid MATCH (ev:Event {event_id: eid}) "
                "CREATE (a)-[:DERIVED_FROM]->(ev)",
                id=assertion_id,
                events=list(evidence_event_ids),
            )
            return {
                "assertion_id": assertion_id,
                "status": "unverified",
                "valid_from": valid_from,
                "valid_to": valid_to,
                "recorded_at": now,
                "bindings": list(bindings),
            }

        return await self.write_once(
            agent_id=agent_id,
            tool="propose_assertion",
            idempotency_key=idempotency_key,
            now=now,
            work=work,
            on_behalf_of=on_behalf_of,
            trigger_event_id=trigger_event_id,
        )

    async def flag(
        self,
        *,
        agent_id: str,
        idempotency_key: str,
        now: datetime,
        assertion_id: str,
        reason: str,
        evidence_event_ids: Sequence[str],
        on_behalf_of: str | None = None,
        trigger_event_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        flag_id = new_id("FLAG")

        async def work(tx: AsyncManagedTransaction) -> dict[str, Any]:
            row = await (
                await tx.run(
                    "MATCH (a:ContextAssertion {assertion_id: $aid}) SET a.review_due = true "
                    "CREATE (f:Flag {flag_id: $id, reason: $reason, agent_id: $agent, "
                    "  flagged_at: $now})-[:FLAGS]->(a) "
                    "WITH a, f OPTIONAL MATCH (ev:Event) WHERE ev.event_id IN $events "
                    "FOREACH (e IN CASE WHEN ev IS NULL THEN [] ELSE [ev] END | "
                    "  CREATE (f)-[:EVIDENCE]->(e)) "
                    "RETURN DISTINCT a.asserted_by AS author",
                    aid=assertion_id,
                    id=flag_id,
                    reason=reason,
                    agent=agent_id,
                    now=now,
                    events=list(evidence_event_ids),
                )
            ).single()
            if row is None:
                raise NotFoundError(f"unknown assertion {assertion_id!r}")
            return {
                "flag_id": flag_id,
                "assertion_id": assertion_id,
                "review_due": True,
                "routed_to": row["author"],
            }

        return await self.write_once(
            agent_id=agent_id,
            tool="flag_assertion",
            idempotency_key=idempotency_key,
            now=now,
            work=work,
            on_behalf_of=on_behalf_of,
            trigger_event_id=trigger_event_id,
        )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


_RANK = {
    Match.DIRECT: 0,
    Match.MODEL: 1,
    Match.SITE: 2,
    Match.SEGMENT: 3,
    Match.SAME_MODEL_DEVICE: 4,
}
_MATCH_BY_RANK = {v: k for k, v in _RANK.items()}


def _firmware_at(
    entity: Mapping[str, Any], upgrades: Sequence[Mapping[str, Any]], t: datetime
) -> tuple[str | None, str | None]:
    """Firmware at t from upgrade events (the as-running layer), else the current value."""
    if entity.get("entity_type") != "device":
        return None, None
    before = [u for u in upgrades if u.get("event_time") and u["event_time"] <= t]
    after = [u for u in upgrades if u.get("event_time") and u["event_time"] > t]
    if before:
        payload = json.loads(before[-1].get("payload_json") or "{}")
        if payload.get("to"):
            return str(payload["to"]), "firmware_upgrade event"
    if after:
        payload = json.loads(after[0].get("payload_json") or "{}")
        if payload.get("from"):
            return str(payload["from"]), "before the next firmware_upgrade event"
    current = entity.get("firmware") or entity.get("firmware_as_built")
    return (str(current), "current inventory") if current else (None, None)


# --- Write-path helpers (run inside a transaction) ------------------------------------------


def _rule[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Apply a domain time rule; a broken invariant rejects the write."""
    try:
        return fn(*args, **kwargs)
    except (rules.TemporalError, ValidationError) as exc:
        raise WriteRejectedError(str(exc)) from exc


async def _single(tx: AsyncManagedTransaction, query: LiteralString, **params: Any) -> Any:
    record = await (await tx.run(query, params)).single()
    return None if record is None else record[0]


async def _state(tx: AsyncManagedTransaction, assertion_id: str) -> rules.TemporalState:
    a = await _single(
        tx,
        "MATCH (a:ContextAssertion {assertion_id: $id}) RETURN a { .valid_from, .valid_to, "
        ".recorded_at, .retracted_at, .status, .review_due } AS a",
        id=assertion_id,
    )
    if a is None:
        raise WriteRejectedError(f"unknown assertion {assertion_id!r}")
    a = py(a)
    a["review_due"] = bool(a.get("review_due"))
    a["status"] = a.get("status") or AssertionStatus.UNVERIFIED
    return rules.TemporalState.model_validate(a)


async def _out(tx: AsyncManagedTransaction, assertion_id: str, *, replayed: bool) -> AssertionOut:
    a = await _single(
        tx, "MATCH (a:ContextAssertion {assertion_id: $id}) RETURN a { .* } AS a", id=assertion_id
    )
    a = py(a)
    return AssertionOut.model_validate(
        {**{k: a.get(k) for k in AssertionOut.model_fields}, "replayed": replayed}
    )


def idempotency_key(completing_event_id: str, statement: str) -> str:
    """Source event id + statement hash: re-extracting the same thread writes nothing new."""
    norm = " ".join(statement.split()).casefold()
    return f"{completing_event_id}:{hashlib.sha256(norm.encode()).hexdigest()[:16]}"


async def _create(tx: AsyncManagedTransaction, item: AssertionIn) -> AssertionOut:
    source_ids = sorted({s.event_id for s in item.sources})
    found = py(
        await _single(
            tx,
            "MATCH (e:Event) WHERE e.event_id IN $ids "
            "RETURN collect({id: e.event_id, t: e.event_time}) AS rows",
            ids=source_ids,
        )
    )
    missing = set(source_ids) - {r["id"] for r in found}
    if missing:
        raise WriteRejectedError(f"unknown source events: {', '.join(sorted(missing))}")
    # R2: recorded when the last source event happened (ties: the larger id, deterministic).
    last = max(found, key=lambda r: (r["t"], r["id"]))
    key = idempotency_key(last["id"], item.statement)
    existing = await _single(
        tx,
        "MATCH (a:ContextAssertion {idempotency_key: $key}) RETURN a.assertion_id AS id",
        key=key,
    )
    if existing is not None:
        return await _out(tx, existing, replayed=True)

    entity_ids = sorted({b.entity_id for b in item.bindings})
    known = await _single(
        tx,
        "MATCH (e:Entity) WHERE e.entity_id IN $ids RETURN collect(e.entity_id) AS ids",
        ids=entity_ids,
    )
    missing = set(entity_ids) - set(known)
    if missing:  # ST-12: never create an orphan entity
        raise WriteRejectedError(f"unknown entities: {', '.join(sorted(missing))}")
    linked = sorted({*item.contradicts, *item.corroborates})
    known = await _single(
        tx,
        "MATCH (a:ContextAssertion) WHERE a.assertion_id IN $ids "
        "RETURN collect(a.assertion_id) AS ids",
        ids=linked,
    )
    missing = set(linked) - set(known)
    if missing:
        raise WriteRejectedError(f"unknown assertions: {', '.join(sorted(missing))}")

    recorded = last["t"]
    # V6: a side that outranks every assertion it contradicts keeps its status.
    disputed = False
    if item.contradicts:
        top = await _single(
            tx,
            "MATCH (a:ContextAssertion) WHERE a.assertion_id IN $ids "
            "RETURN max(coalesce(a.authority, 1)) AS top",
            ids=item.contradicts,
        )
        mine = item.authority if item.authority is not None else 1
        disputed = mine <= (1 if top is None else top)
    state = _rule(
        rules.TemporalState,
        valid_from=item.valid_from or recorded,
        valid_to=item.valid_to,
        recorded_at=recorded,
        status=AssertionStatus.DISPUTED if disputed else item.status,
    )
    assertion_id = new_id("ASR")
    await tx.run(
        "MATCH (last:Event {event_id: $last}) "
        "CREATE (a:ContextAssertion {assertion_id: $id, statement: $statement, topic: $topic, "
        "  kind: $kind, qualifiers_json: $qualifiers, origin: $origin, status: $status, "
        "  confidence: $confidence, valid_from: $vf, valid_to: $vt, recorded_at: $recorded, "
        "  review_due: false, support_n: 0, refute_n: 0, idempotency_key: $key, "
        "  asserted_by: last.author, authority: $authority}) "
        "WITH a UNWIND $bindings AS b MATCH (e:Entity {entity_id: b.entity_id}) "
        "CREATE (a)-[:ABOUT {scope: b.scope, resolution_confidence: b.resolution_confidence, "
        "  surface_form: b.surface_form, candidate: b.candidate}]->(e)",
        last=last["id"],
        id=assertion_id,
        statement=item.statement,
        topic=item.topic,
        kind=item.kind,
        qualifiers=json.dumps(item.qualifiers, sort_keys=True),
        origin=item.origin,
        status=state.status.value,
        confidence=item.confidence,
        vf=state.valid_from,
        vt=state.valid_to,
        recorded=state.recorded_at,
        key=key,
        authority=item.authority,
        bindings=[b.model_dump(mode="json") for b in item.bindings],
    )
    await tx.run(
        "MATCH (a:ContextAssertion {assertion_id: $id}) "
        "UNWIND $sources AS s MATCH (ev:Event {event_id: s.event_id}) "
        "CREATE (a)-[:DERIVED_FROM {quote: s.quote}]->(ev) "
        "WITH ev WHERE ev.author IS NOT NULL "
        "MERGE (p:Person {person_id: ev.author}) MERGE (ev)-[:AUTHORED_BY]->(p)",
        id=assertion_id,
        sources=[s.model_dump() for s in item.sources],
    )
    # Symmetric relations: one edge per pair, read undirected (as the seed writes them).
    # Disagreement makes both sides disputed (V6).
    await tx.run(
        "MATCH (a:ContextAssertion {assertion_id: $id}) "
        "CALL (a) { UNWIND $contradicts AS x MATCH (b:ContextAssertion {assertion_id: x}) "
        "  MERGE (a)-[:CONTRADICTS]->(b) "
        "  SET b.status = CASE WHEN b.status IN ['superseded', 'expired'] THEN b.status "
        "    WHEN coalesce(b.authority, 1) > coalesce(a.authority, 1) THEN b.status "
        "    ELSE 'disputed' END } "
        "CALL (a) { UNWIND $corroborates AS x MATCH (b:ContextAssertion {assertion_id: x}) "
        "  MERGE (a)-[:CORROBORATES]->(b) }",
        id=assertion_id,
        contradicts=item.contradicts,
        corroborates=item.corroborates,
    )
    return AssertionOut(
        assertion_id=assertion_id,
        status=state.status,
        valid_from=state.valid_from,
        valid_to=state.valid_to,
        recorded_at=state.recorded_at,
        idempotency_key=key,
    )


# V5: the temporal invariants (R2-R7, verify_temporal), checked on the graph as the
# transaction left it. Gold rows have no source events; proposals are recorded when proposed.
_V5: LiteralString = """
UNWIND $ids AS id
MATCH (a:ContextAssertion {assertion_id: id})
OPTIONAL MATCH (a)-[:DERIVED_FROM]->(ev:Event)
WITH a, max(ev.event_time) AS last_source, count(ev) AS n_sources
OPTIONAL MATCH (a)-[:ABOUT]->(e:Entity)
WITH a, last_source, n_sources, count(e) AS n_bindings
OPTIONAL MATCH (succ:ContextAssertion)-[:SUPERSEDES]->(a) WHERE succ.retracted_at IS NULL
WITH a, last_source, n_sources, n_bindings, collect(succ.valid_from) AS succ_from
RETURN a.assertion_id AS id, [x IN [
  CASE WHEN a.valid_to IS NOT NULL AND a.valid_to <= a.valid_from
    THEN 'valid_to must be after valid_from' END,
  CASE WHEN a.retracted_at IS NOT NULL AND a.retracted_at < a.recorded_at
    THEN 'retracted before it was recorded' END,
  CASE WHEN a.status IN ['superseded', 'expired'] AND a.valid_to IS NULL
    THEN a.status + ' without valid_to' END,
  CASE WHEN n_bindings = 0 THEN 'bound to no entity' END,
  CASE WHEN coalesce(a.origin, '') <> 'gold' AND n_sources = 0 THEN 'derived from no event' END,
  CASE WHEN a.origin = 'extracted' AND a.recorded_at <> last_source
    THEN 'recorded_at is not the last source event_time (R2)' END,
  CASE WHEN size(succ_from) > 1 THEN 'superseded more than once' END,
  CASE WHEN size(succ_from) = 1 AND a.valid_to <> succ_from[0]
    THEN 'valid_to is not the successor valid_from (R4b)' END
] WHERE x IS NOT NULL] AS problems
"""


async def _check_v5(tx: AsyncManagedTransaction, ids: Sequence[str]) -> None:
    rows = await (await tx.run(_V5, ids=sorted(set(ids)))).data()
    problems = [f"{r['id']}: {p}" for r in rows for p in r["problems"]]
    if problems:
        raise WriteRejectedError("temporal check failed (V5): " + "; ".join(problems))


async def _evidence(
    tx: AsyncManagedTransaction,
    outcome_id: str,
    assertion_id: str,
    relation: str | None,
    conditions_met: bool | None,
    review: bool,
) -> dict[str, Any]:
    """Outcome -[SUPPORTS|REFUTES]-> assertion and its counters (plan 001 §6.2). An edge that
    already exists is not counted again."""
    row = await (
        await tx.run(
            "MATCH (o:Outcome {event_id: $id}), (a:ContextAssertion {assertion_id: $aid}) "
            "FOREACH (_ IN CASE WHEN $rel = 'SUPPORTS' AND NOT EXISTS { (o)-[:SUPPORTS]->(a) } "
            "  THEN [1] ELSE [] END | "
            "  CREATE (o)-[:SUPPORTS {conditions_met: $met}]->(a) "
            "  SET a.support_n = coalesce(a.support_n, 0) + 1) "
            "FOREACH (_ IN CASE WHEN $rel = 'REFUTES' AND NOT EXISTS { (o)-[:REFUTES]->(a) } "
            "  THEN [1] ELSE [] END | "
            "  CREATE (o)-[:REFUTES {conditions_met: $met}]->(a) "
            "  SET a.refute_n = coalesce(a.refute_n, 0) + 1) "
            "FOREACH (_ IN CASE WHEN $review THEN [1] ELSE [] END | "
            "  SET a.review_due = true) "
            "RETURN coalesce(a.support_n, 0) AS support_n, "
            "coalesce(a.refute_n, 0) AS refute_n, coalesce(a.review_due, false) AS review",
            id=outcome_id,
            aid=assertion_id,
            rel=relation,
            met=conditions_met,
            review=review,
        )
    ).single()
    if row is None:
        raise NotFoundError(f"unknown outcome {outcome_id!r} or assertion {assertion_id!r}")
    return dict(row)
