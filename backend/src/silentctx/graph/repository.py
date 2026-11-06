"""ContextRepository: the one place that reads and writes the context layer in Neo4j.

Reads implement the as-of rules (plan 001 R9/R10):
  in scope at t   <=> valid_from <= t < coalesce(valid_to, inf)
  known at k      <=> recorded_at <= k and (retracted_at is null or retracted_at > k)

Writes (agents, via MCP) run in one transaction each and are idempotent: a WriteReceipt node
keyed by agent, tool and idempotency key stores the first result, and a replay returns it.
Every write also records an AgentAction event (R1: every write is itself an event).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, LiteralString

from neo4j import AsyncDriver, AsyncManagedTransaction
from neo4j.exceptions import ConstraintError
from neo4j.time import DateTime as Neo4jDateTime

from silentctx.context.models import (
    AccessRecordView,
    AssertionView,
    EntityView,
    EventView,
    EvidenceView,
    FlagView,
    Match,
    SegmentRef,
)
from silentctx.domain.models import AssertionStatus
from silentctx.graph.cypher import vector

END_OF_TIME: LiteralString = "datetime('9999-12-31T00:00:00Z')"

# Ranking (plan 002 §4.1 "ranked"): how the assertion reached the entity, its status and its
# confidence. Shown to agents as `score`; never used to hide anything.
MATCH_WEIGHT = {
    Match.DIRECT: 1.0,
    Match.MODEL: 0.9,
    Match.SEGMENT: 0.8,
    Match.SAME_MODEL_DEVICE: 0.5,
}
STATUS_WEIGHT = {
    AssertionStatus.BELIEVED_TRUE: 1.0,
    AssertionStatus.UNVERIFIED: 0.8,
    AssertionStatus.DISPUTED: 0.7,
    AssertionStatus.SUPERSEDED: 0.3,
    AssertionStatus.EXPIRED: 0.3,
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
  [(a)-[b:ABOUT]->(e:Entity) | {entity_id: e.entity_id, scope: b.scope}] AS bindings,
  [(a)-[:CONTRADICTS]-(x:ContextAssertion) | x.assertion_id] AS contradicts,
  [(a)-[:CORROBORATES]-(x:ContextAssertion) | x.assertion_id] AS corroborates,
  head([(x:ContextAssertion)-[:SUPERSEDES]->(a) | x.assertion_id]) AS superseded_by,
  head([(x:ContextAssertion)-[:SUPERSEDES]->(a) | x.recorded_at]) AS superseded_recorded_at,
  head([(a)-[:SUPERSEDES]->(x:ContextAssertion) | x.assertion_id]) AS supersedes,
  [(a)-[:DERIVED_FROM]->(ev:Event) | ev { .event_id, .event_time, .platform, .author,
      .channel, .permalink, .text }] AS sources
"""

_KNOWN: LiteralString = "a.recorded_at <= $k AND (a.retracted_at IS NULL OR a.retracted_at > $k)"
# R10: validity as it was known at k. A supersession is recorded when its successor is
# (R2), so if the successor was recorded after k, the predecessor was still open-ended then:
# "why did the AI dispatch to Kemi on 08-10 with the padlock key?"
_VALID_TO_AT_K: LiteralString = (
    "CASE WHEN EXISTS { MATCH (succ:ContextAssertion)-[:SUPERSEDES]->(a) "
    "WHERE succ.recorded_at > $k } THEN null ELSE a.valid_to END"
)
_VALID: LiteralString = (
    "a.valid_from <= $t AND $t < coalesce(" + _VALID_TO_AT_K + ", " + END_OF_TIME + ")"
)


def assertion_view(
    record: Mapping[str, Any], match: Match | None = None, known_at: datetime | None = None
) -> AssertionView:
    """One assertion. With `known_at`, as it was on record then (R10): a supersession
    recorded later is not applied (open-ended, believed true, no successor yet)."""
    a = py(dict(record["a"]))
    superseded_by = record.get("superseded_by")
    succ_recorded = py(record.get("superseded_recorded_at"))
    if known_at is not None and succ_recorded is not None and succ_recorded > known_at:
        a["valid_to"] = None
        if a.get("status") == "superseded":
            a["status"] = "believed_true"
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
    for src in py(record.get("sources") or []):
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
            properties={k: v for k, v in e.items() if k in keep and v is not None},
        )

    async def scope(self, entity_id: str, t: datetime) -> dict[str, Match]:
        """The entity, its model and generation, and the segments connecting it, at t."""
        rows = await self._read(
            "MATCH (e:Entity {entity_id: $id}) "
            "OPTIONAL MATCH (e)-[im:IS_MODEL]->(m:HwModel) "
            "  WHERE im.valid_from <= $t AND $t < coalesce(im.valid_to, " + END_OF_TIME + ") "
            "OPTIONAL MATCH (m)-[:OF_GENERATION]->(g:Generation) "
            "OPTIONAL MATCH (e)-[:OF_GENERATION]->(own:Generation) "
            "OPTIONAL MATCH (s:Segment)-[c:CONNECTS]->(e) "
            "  WHERE c.valid_from <= $t AND $t < coalesce(c.valid_to, " + END_OF_TIME + ") "
            "RETURN e.entity_id AS e, m.entity_id AS m, "
            "coalesce(g.entity_id, own.entity_id) AS g, "
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
            "MATCH (a:ContextAssertion)-[:ABOUT]->(:Entity {entity_id: s.id}) "
            "WHERE " + _VALID + " AND " + _KNOWN + " "
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
                "MATCH (a:ContextAssertion)-[:ABOUT {scope: 'observed_on'}]->(o:Device)"
                "-[im:IS_MODEL]->(:HwModel {entity_id: $model}) "
                "WHERE NOT o.entity_id IN $ids AND " + _VALID + " AND " + _KNOWN + " "
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
            "MATCH (a:ContextAssertion)-[:ABOUT]->(:Entity {entity_id: id}) "
            "WHERE (a.topic CONTAINS 'access' OR a.topic CONTAINS 'site') "
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
            action_id = new_id("ACT")
            await tx.run(
                "CREATE (r:WriteReceipt {key: $key, result: $result, created_at: $now}) "
                "CREATE (e:Event:AgentAction {event_id: $action, event_time: $now, "
                "  platform: 'mcp', tool: $tool, agent_id: $agent, on_behalf_of: $behalf, "
                "  idempotency_key: $ikey, ingested_at: $now}) "
                "CREATE (e)-[:RECEIPT]->(r)",
                key=key,
                result=json.dumps(result, default=_json_default),
                now=now,
                action=action_id,
                tool=tool,
                agent=agent_id,
                behalf=on_behalf_of,
                ikey=idempotency_key,
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
            counters = await (
                await tx.run(
                    "MATCH (o:Outcome {event_id: $id}), (a:ContextAssertion {assertion_id: $aid}) "
                    "FOREACH (_ IN CASE WHEN $rel = 'SUPPORTS' THEN [1] ELSE [] END | "
                    "  CREATE (o)-[:SUPPORTS {conditions_met: $met}]->(a) "
                    "  SET a.support_n = coalesce(a.support_n, 0) + 1) "
                    "FOREACH (_ IN CASE WHEN $rel = 'REFUTES' THEN [1] ELSE [] END | "
                    "  CREATE (o)-[:REFUTES {conditions_met: $met}]->(a) "
                    "  SET a.refute_n = coalesce(a.refute_n, 0) + 1) "
                    "FOREACH (_ IN CASE WHEN $review THEN [1] ELSE [] END | "
                    "  SET a.review_due = true) "
                    "RETURN coalesce(a.support_n, 0) AS support_n, "
                    "coalesce(a.refute_n, 0) AS refute_n, coalesce(a.review_due, false) AS review",
                    id=event_id,
                    aid=assertion_id,
                    rel=relation,
                    met=conditions_met,
                    review=review,
                )
            ).single()
            if counters is None:
                raise NotFoundError(f"unknown assertion {assertion_id!r}")
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
        )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


_RANK = {Match.DIRECT: 0, Match.MODEL: 1, Match.SEGMENT: 2, Match.SAME_MODEL_DEVICE: 3}
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
