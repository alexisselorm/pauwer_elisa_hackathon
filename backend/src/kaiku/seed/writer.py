"""Write the network layers and the gold layer into Neo4j (plan 001 §5).

Every write is `UNWIND $rows ... MERGE` on a stable key followed by `SET`, so a re-run
changes nothing. Validity instants are stored as Neo4j DateTime (UTC), which the range
indexes and the as-of queries (R9) compare natively.

Graph shape:
  (:Entity:Device|Segment|Site|HwModel|Generation|Topic {entity_id, valid_from, valid_to})
  (Device)-[:IS_MODEL]->(HwModel)-[:OF_GENERATION]->(Generation)
  (Segment)-[:CONNECTS {end}]->(Device)            segment endpoints that are entities
  (Device)-[:LINKED {link_id}]->(Device)           as-running links, current and retired
  (:RunningSnapshot)-[:SNAPSHOT_OF]->(Device)      as-running hardware snapshots
  (:Event:DeviceEvent|Alarm {event_id, event_time})-[:ON]->(Device)
  gold only: (:ContextAssertion {origin:'gold'})-[:ABOUT {scope}]->(:Entity),
             SUPERSEDES / CONTRADICTS / CORROBORATES, (:AccessRecord)-[:FOR]->(Segment)
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, LiteralString

from neo4j import AsyncDriver
from pydantic import BaseModel

from kaiku.seed.models import EntityRow
from kaiku.seed.sources import MockData

GOLD = "gold"
SEED = "seed"
GENERATIONS = ("Gen1", "Gen2", "Gen3")

# Gold subject kind -> binding scope (plan 001 §5: observed_on | applies_to | activity).
_SCOPE_BY_ENTITY_TYPE = {
    "device": "observed_on",
    "fibre": "observed_on",
    "microwave": "observed_on",
    "site": "observed_on",
    "hw_model": "applies_to",
    "topic": "activity",
}


class SeedReport(BaseModel):
    """Counts of rows written (not of rows changed: writes are idempotent)."""

    entities: int = 0
    is_model: int = 0
    connects: int = 0
    connects_skipped_endpoints: list[str] = []
    links: int = 0
    links_skipped: list[str] = []
    snapshots: int = 0
    device_events: int = 0
    alarms: int = 0
    gold_assertions: int = 0
    gold_bindings: int = 0
    access_records: int = 0
    pruned: dict[str, int] = {}


async def _run(
    driver: AsyncDriver, database: str, query: LiteralString, rows: Sequence[Any]
) -> None:
    if rows:
        await driver.execute_query(query, rows=list(rows), database_=database)


def _validity(row: BaseModel) -> dict[str, Any]:
    data = row.model_dump()
    return {"valid_from": data["valid_from"], "valid_to": data["valid_to"]}


# --- Network layers --------------------------------------------------------------------------

_ENTITY_LABEL: dict[str, LiteralString] = {
    "device": "Device",
    "fibre": "Segment",
    "microwave": "Segment",
    "site": "Site",
    "hw_model": "HwModel",
    "topic": "Topic",
}

_MERGE_ENTITIES: dict[LiteralString, LiteralString] = {
    label: (
        "UNWIND $rows AS r MERGE (n:Entity {entity_id: r.entity_id}) SET n:"
        + label
        + ", n += r.props"
    )
    for label in ("Device", "Segment", "Site", "HwModel", "Topic", "Generation")
}


def _entity_props(e: EntityRow, extra: dict[str, Any]) -> dict[str, Any]:
    # exclude_none: an open-ended valid_to is stored as an absent property (Cypher null).
    base = e.model_dump(exclude={"entity_id", "type"}, exclude_none=True)
    if e.type in ("fibre", "microwave"):
        base["segment_type"] = e.type
    return {**base, **extra, "entity_type": e.type, "origin": SEED}


async def write_network(driver: AsyncDriver, database: str, data: MockData) -> SeedReport:
    report = SeedReport()
    entities = data.entities()
    ids = {e.entity_id for e in entities}
    devices = {d.device_id: d for d in data.devices()}
    running = {n.node_id: n for n in data.nodes_running()}
    segments = {s.segment_id: s for s in data.segments()}

    by_label: dict[LiteralString, list[dict[str, Any]]] = {}
    for e in entities:
        extra: dict[str, Any] = {}
        if e.type == "device":
            d, n = devices.get(e.entity_id), running.get(e.entity_id)
            if d:
                extra |= d.model_dump(
                    include={"serial", "chassis", "rack", "room", "warranty_end"}, exclude_none=True
                )
                if d.firmware:
                    extra["firmware_as_built"] = d.firmware
            if n:
                extra["running_status"] = n.status
                if n.firmware:
                    extra["firmware"] = n.firmware
        if e.type in ("fibre", "microwave") and (s := segments.get(e.entity_id)):
            extra |= s.model_dump(
                include={"length_km", "owner", "cable_ref", "barrier"}, exclude_none=True
            )
        by_label.setdefault(_ENTITY_LABEL[e.type], []).append(
            {"entity_id": e.entity_id, "props": _entity_props(e, extra)}
        )
    epoch = min(e.valid_from for e in entities)
    by_label["Generation"] = [
        {
            "entity_id": g,
            "props": {"name": g, "entity_type": "generation", "origin": SEED, "valid_from": epoch},
        }
        for g in GENERATIONS
    ]
    for label, rows in by_label.items():
        await _run(driver, database, _MERGE_ENTITIES[label], rows)
        report.entities += len(rows)

    # Device -> HwModel (valid while the device is), HwModel -> Generation.
    model_rows = [
        {"device": e.entity_id, "model": e.model, **_validity(e)}
        for e in entities
        if e.type == "device" and e.model in ids
    ]
    await _run(
        driver,
        database,
        "UNWIND $rows AS r MATCH (d:Device {entity_id: r.device}), "
        "(m:HwModel {entity_id: r.model}) "
        "MERGE (d)-[x:IS_MODEL]->(m) SET x.valid_from = r.valid_from, x.valid_to = r.valid_to",
        model_rows,
    )
    report.is_model = len(model_rows)
    gen_rows = [
        {"model": e.entity_id, "generation": e.generation}
        for e in entities
        if e.type == "hw_model" and e.generation in GENERATIONS
    ]
    await _run(
        driver,
        database,
        "UNWIND $rows AS r MATCH (m:HwModel {entity_id: r.model}), "
        "(g:Generation {entity_id: r.generation}) "
        "MERGE (m)-[:OF_GENERATION]->(g)",
        gen_rows,
    )

    # Segment -> endpoint devices (only endpoints that are entities).
    connects: list[dict[str, Any]] = []
    for s in segments.values():
        for end, device in (("a", s.end_a), ("b", s.end_b)):
            if device in ids:
                connects.append(
                    {"segment": s.segment_id, "device": device, "end": end, **_validity(s)}
                )
            else:
                report.connects_skipped_endpoints.append(device)
    await _run(
        driver,
        database,
        "UNWIND $rows AS r MATCH (s:Segment {entity_id: r.segment}), "
        "(d:Device {entity_id: r.device}) "
        "MERGE (s)-[x:CONNECTS {end: r.end}]->(d) "
        "SET x.valid_from = r.valid_from, x.valid_to = r.valid_to",
        connects,
    )
    report.connects = len(connects)

    links: list[dict[str, Any]] = []
    for link in data.links():
        if link.end_a in ids and link.end_b in ids:
            links.append(
                {
                    "a": link.end_a,
                    "b": link.end_b,
                    "props": link.model_dump(exclude={"end_a", "end_b"}, exclude_none=True)
                    | _validity(link),
                }
            )
        else:
            report.links_skipped.append(link.link_id)
    await _run(
        driver,
        database,
        "UNWIND $rows AS r MATCH (a:Device {entity_id: r.a}), (b:Device {entity_id: r.b}) "
        "MERGE (a)-[x:LINKED {link_id: r.props.link_id}]->(b) SET x += r.props, "
        "x.valid_to = r.props.valid_to",
        links,
    )
    report.links = len(links)

    aliases = data.model_aliases()
    snapshots: list[dict[str, Any]] = [
        {
            "device": s.physical_name,
            "props": s.model_dump(exclude={"physical_name"}, exclude_none=True)
            | _validity(s)
            | {
                "snapshot_id": s.physical_node_id,
                "origin": SEED,
                "hw_model": aliases.get((s.hardware_model or "").casefold()),
                "hw_model_raw": s.hardware_model,
            },
        }
        for s in data.snapshots()
        if s.physical_name in ids
    ]
    await _run(
        driver,
        database,
        "UNWIND $rows AS r MATCH (d:Device {entity_id: r.device}) "
        "MERGE (s:RunningSnapshot {snapshot_id: r.props.snapshot_id}) SET s += r.props, "
        "s.valid_to = r.props.valid_to "
        "MERGE (s)-[:SNAPSHOT_OF]->(d)",
        snapshots,
    )
    report.snapshots = len(snapshots)

    events: list[dict[str, Any]] = [
        {
            "device": ev.device_id,
            "props": {
                "event_id": ev.event_id,
                "event_time": ev.event_time,
                "end_time": ev.end_time,
                "platform": "device_event",
                "type": ev.type,
                "workorder_id": ev.workorder_id,
                "outcome": ev.outcome,
                "description": ev.description,
                "team": ev.team,
                "payload_json": json.dumps(ev.payload, sort_keys=True),
                "origin": SEED,
            },
        }
        for ev in data.device_events()
    ]
    await _run(
        driver,
        database,
        "UNWIND $rows AS r MERGE (e:Event {event_id: r.props.event_id}) "
        "SET e:DeviceEvent, e += r.props "
        "WITH e, r MATCH (d:Device {entity_id: r.device}) MERGE (e)-[:ON]->(d)",
        events,
    )
    report.device_events = len(events)

    alarms: list[dict[str, Any]] = [
        {
            "device": a.ne_id,
            "props": {
                "event_id": a.alarm_id,
                "event_time": a.raised,
                "cleared_at": a.cleared,
                "platform": "alarm",
                "type": a.type,
                "severity": a.severity,
                "probable_cause": a.probable_cause,
                "text": a.text,
                "state": a.state,
                "origin": SEED,
            },
        }
        for a in data.alarms()
    ]
    await _run(
        driver,
        database,
        "UNWIND $rows AS r MERGE (e:Event {event_id: r.props.event_id}) SET e:Alarm, e += r.props "
        "WITH e, r MATCH (d:Device {entity_id: r.device}) MERGE (e)-[:ON]->(d)",
        alarms,
    )
    report.alarms = len(alarms)

    # Converge on the source: remove seed-owned rows that are no longer in it.
    entity_ids = [r["entity_id"] for rows in by_label.values() for r in rows]
    event_ids = [r["props"]["event_id"] for r in events + alarms]
    report.pruned = await _prune(
        driver,
        database,
        {
            "entities": (_PRUNE_ENTITIES, entity_ids),
            "events": (_PRUNE_EVENTS, event_ids),
            "snapshots": (_PRUNE_SNAPSHOTS, [r["props"]["snapshot_id"] for r in snapshots]),
            "is_model": (_PRUNE_IS_MODEL, [f"{r['device']}|{r['model']}" for r in model_rows]),
            "connects": (
                _PRUNE_CONNECTS,
                [f"{r['segment']}|{r['device']}|{r['end']}" for r in connects],
            ),
            "links": (_PRUNE_LINKS, [r["props"]["link_id"] for r in links]),
            "event_on": (
                _PRUNE_EVENT_ON,
                [f"{r['props']['event_id']}|{r['device']}" for r in events + alarms],
            ),
        },
    )
    return report


# --- Gold layer (eval only) -----------------------------------------------------------------


async def write_gold(driver: AsyncDriver, database: str, data: MockData) -> SeedReport:
    report = SeedReport()
    entity_type = {e.entity_id: e.type for e in data.entities()}
    gold = data.gold_assertions()

    rows: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for a in gold:
        rows.append(
            {
                "assertion_id": a.assertion_id,
                "origin": GOLD,
                "topic": a.topic,
                "statement": a.statement,
                "status": a.status.value,
                "confidence_label": a.confidence,
                "valid_from": a.valid_from,
                "valid_to": a.valid_to,
                "recorded_at": a.recorded_at,
                "source_type": a.source_type,
                "source_ref": a.source_ref,
                "source_detail": a.source_detail,
                "asserted_by": a.asserted_by,
                "qualifiers_json": json.dumps(a.qualifiers, sort_keys=True),
                "review_due": False,
            }
        )
        subject_type = entity_type.get(a.subject_entity_id)
        if subject_type is None:
            raise ValueError(f"{a.assertion_id}: unknown subject {a.subject_entity_id}")
        targets = {(a.subject_entity_id, _SCOPE_BY_ENTITY_TYPE[subject_type])}
        models = a.qualifiers.get("applies_to_models") or []
        targets |= {(m, "applies_to") for m in models if m in entity_type}
        topic_id = f"TOPIC:{a.topic}"
        if topic_id in entity_type:
            targets.add((topic_id, "activity"))
        bindings += [
            {"assertion_id": a.assertion_id, "entity_id": eid, "scope": scope}
            for eid, scope in sorted(targets)
        ]

    await _run(
        driver,
        database,
        "UNWIND $rows AS r MERGE (a:ContextAssertion {assertion_id: r.assertion_id}) "
        "SET a += r, a.valid_to = r.valid_to",
        rows,
    )
    await _run(
        driver,
        database,
        "UNWIND $rows AS r MATCH (a:ContextAssertion {assertion_id: r.assertion_id}), "
        "(e:Entity {entity_id: r.entity_id}) "
        "MERGE (a)-[x:ABOUT {scope: r.scope}]->(e) "
        "SET x.resolution_confidence = 1.0, x.origin = 'gold'",
        bindings,
    )
    report.gold_assertions, report.gold_bindings = len(rows), len(bindings)

    supersedes = [{"succ": a.assertion_id, "pred": a.supersedes} for a in gold if a.supersedes]
    supersedes += [
        {"succ": a.superseded_by, "pred": a.assertion_id} for a in gold if a.superseded_by
    ]
    await _run(
        driver,
        database,
        "UNWIND $rows AS r MATCH (s:ContextAssertion {assertion_id: r.succ}), "
        "(p:ContextAssertion {assertion_id: r.pred}) MERGE (s)-[:SUPERSEDES]->(p)",
        supersedes,
    )
    # Symmetric relations: one edge per unordered pair (query them undirected).
    for rel, attr in (("CONTRADICTS", "contradicts"), ("CORROBORATES", "corroborates")):
        pairs = sorted({tuple(sorted((a.assertion_id, b))) for a in gold for b in getattr(a, attr)})
        query: LiteralString = (
            "UNWIND $rows AS r MATCH (x:ContextAssertion {assertion_id: r[0]}), "
            "(y:ContextAssertion {assertion_id: r[1]}) MERGE (x)-[:" + rel + "]->(y)"
        )
        await _run(driver, database, query, [list(p) for p in pairs])

    access: list[dict[str, Any]] = [
        {
            "segment": r.segment_id,
            "props": r.model_dump(exclude={"segment_id"})
            | {"record_id": r.record_id, "origin": GOLD},
        }
        for r in data.access_register()
    ]
    await _run(
        driver,
        database,
        "UNWIND $rows AS r MATCH (s:Segment {entity_id: r.segment}) "
        "MERGE (x:AccessRecord {record_id: r.props.record_id}) SET x += r.props "
        "MERGE (x)-[:FOR]->(s)",
        access,
    )
    report.access_records = len(access)
    report.pruned = await _prune(
        driver,
        database,
        {
            "gold_assertions": (_PRUNE_GOLD_ASSERTIONS, [r["assertion_id"] for r in rows]),
            "gold_bindings": (
                _PRUNE_GOLD_BINDINGS,
                [f"{b['assertion_id']}|{b['entity_id']}|{b['scope']}" for b in bindings],
            ),
            "access_records": (_PRUNE_ACCESS, [r["props"]["record_id"] for r in access]),
        },
    )
    return report


# --- Pruning ----------------------------------------------------------------------------------
# Each query deletes the seed- or gold-owned rows whose key is not in $keep, and returns
# how many it deleted. Extracted knowledge (origin extracted/agent_proposal) is never touched.

_PRUNE_ENTITIES: LiteralString = (
    "MATCH (n:Entity {origin: 'seed'}) WHERE NOT n.entity_id IN $keep "
    "DETACH DELETE n RETURN count(*) AS n"
)
_PRUNE_EVENTS: LiteralString = (
    "MATCH (n:Event {origin: 'seed'}) WHERE NOT n.event_id IN $keep "
    "DETACH DELETE n RETURN count(*) AS n"
)
_PRUNE_SNAPSHOTS: LiteralString = (
    "MATCH (n:RunningSnapshot {origin: 'seed'}) WHERE NOT n.snapshot_id IN $keep "
    "DETACH DELETE n RETURN count(*) AS n"
)
_PRUNE_IS_MODEL: LiteralString = (
    "MATCH (d:Device)-[r:IS_MODEL]->(m:HwModel) "
    "WHERE NOT d.entity_id + '|' + m.entity_id IN $keep DELETE r RETURN count(*) AS n"
)
_PRUNE_CONNECTS: LiteralString = (
    "MATCH (s:Segment)-[r:CONNECTS]->(d:Device) "
    "WHERE NOT s.entity_id + '|' + d.entity_id + '|' + r.end IN $keep "
    "DELETE r RETURN count(*) AS n"
)
_PRUNE_LINKS: LiteralString = (
    "MATCH ()-[r:LINKED]->() WHERE NOT r.link_id IN $keep DELETE r RETURN count(*) AS n"
)
_PRUNE_EVENT_ON: LiteralString = (
    "MATCH (e:Event {origin: 'seed'})-[r:ON]->(d:Device) "
    "WHERE NOT e.event_id + '|' + d.entity_id IN $keep DELETE r RETURN count(*) AS n"
)
_PRUNE_GOLD_ASSERTIONS: LiteralString = (
    "MATCH (n:ContextAssertion {origin: 'gold'}) WHERE NOT n.assertion_id IN $keep "
    "DETACH DELETE n RETURN count(*) AS n"
)
_PRUNE_GOLD_BINDINGS: LiteralString = (
    "MATCH (a:ContextAssertion {origin: 'gold'})-[r:ABOUT]->(e:Entity) "
    "WHERE NOT a.assertion_id + '|' + e.entity_id + '|' + r.scope IN $keep "
    "DELETE r RETURN count(*) AS n"
)
_PRUNE_ACCESS: LiteralString = (
    "MATCH (n:AccessRecord {origin: 'gold'}) WHERE NOT n.record_id IN $keep "
    "DETACH DELETE n RETURN count(*) AS n"
)


async def _prune(
    driver: AsyncDriver, database: str, queries: dict[str, tuple[LiteralString, list[str]]]
) -> dict[str, int]:
    pruned: dict[str, int] = {}
    for name, (query, keep) in queries.items():
        records, _, _ = await driver.execute_query(query, keep=keep, database_=database)
        if count := int(records[0]["n"]):
            pruned[name] = count
    return pruned


async def wipe_gold(driver: AsyncDriver, database: str) -> int:
    """Remove every gold node (and its relationships) before scoring extraction."""
    records, _, _ = await driver.execute_query(
        "MATCH (n) WHERE n.origin = 'gold' "
        "WITH n, count(*) AS c DETACH DELETE n RETURN sum(c) AS n",
        database_=database,
    )
    return int(records[0]["n"] or 0)
