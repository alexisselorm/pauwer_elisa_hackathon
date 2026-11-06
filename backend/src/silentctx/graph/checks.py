"""Temporal invariants as Cypher (plan 001 §3.3, §6.1 V5). Run after seeding; BE-1.7 adds
the checks for extracted knowledge and runs them inside the write transaction.

Each check returns the offending ids; an empty result means the graph satisfies it.
Windows are half-open [valid_from, valid_to), null valid_to = open-ended.
"""

from __future__ import annotations

from typing import LiteralString

from neo4j import AsyncDriver
from pydantic import BaseModel

END_OF_TIME: LiteralString = "datetime('9999-12-31T00:00:00Z')"


class CheckResult(BaseModel):
    name: str
    violations: int
    examples: list[str]


CHECKS: dict[str, LiteralString] = {
    # A window must not be empty or reversed.
    "node_window_order": (
        "MATCH (n) WHERE n.valid_from IS NOT NULL AND n.valid_to IS NOT NULL "
        "AND n.valid_to <= n.valid_from "
        "RETURN coalesce(n.entity_id, n.assertion_id, n.snapshot_id, n.record_id) AS id"
    ),
    "relationship_window_order": (
        "MATCH ()-[r]->() WHERE r.valid_from IS NOT NULL AND r.valid_to IS NOT NULL "
        "AND r.valid_to <= r.valid_from RETURN type(r) + ':' + elementId(r) AS id"
    ),
    # A topology edge can't start before, or after the end of, either endpoint.
    "edge_within_endpoints": (
        "MATCH (a:Entity)-[r:IS_MODEL|CONNECTS|LINKED]->(b:Entity) "
        "WHERE r.valid_from < a.valid_from OR r.valid_from < b.valid_from "
        "OR r.valid_from >= coalesce(a.valid_to, " + END_OF_TIME + ") "
        "OR r.valid_from >= coalesce(b.valid_to, " + END_OF_TIME + ") "
        "RETURN a.entity_id + '-' + type(r) + '->' + b.entity_id AS id"
    ),
    # As-running snapshots of one device form a non-overlapping chain inside its life.
    "snapshot_within_device": (
        "MATCH (s:RunningSnapshot)-[:SNAPSHOT_OF]->(d:Device) "
        "WHERE s.valid_from < d.valid_from "
        "OR s.valid_from >= coalesce(d.valid_to, " + END_OF_TIME + ") "
        "RETURN s.snapshot_id AS id"
    ),
    "snapshot_overlap": (
        "MATCH (s1:RunningSnapshot)-[:SNAPSHOT_OF]->(d:Device)"
        "<-[:SNAPSHOT_OF]-(s2:RunningSnapshot) "
        "WHERE s1.snapshot_id < s2.snapshot_id "
        "AND s1.valid_from < coalesce(s2.valid_to, " + END_OF_TIME + ") "
        "AND s2.valid_from < coalesce(s1.valid_to, " + END_OF_TIME + ") "
        "RETURN s1.snapshot_id + '/' + s2.snapshot_id AS id"
    ),
    # An observed event happens while its device exists (record time = event time).
    "event_within_device": (
        "MATCH (e:Event)-[:ON]->(d:Device) "
        "WHERE e.event_time < d.valid_from "
        "OR e.event_time > coalesce(d.valid_to, " + END_OF_TIME + ") "
        "RETURN e.event_id AS id"
    ),
    # R4b: a successor's valid_from is its predecessor's valid_to (the superseded timestamp).
    "supersession_timestamp": (
        "MATCH (s:ContextAssertion)-[:SUPERSEDES]->(p:ContextAssertion) "
        "WHERE p.valid_to IS NULL OR p.valid_to <> s.valid_from "
        "RETURN s.assertion_id + '>' + p.assertion_id AS id"
    ),
    "superseded_has_successor": (
        "MATCH (p:ContextAssertion {status: 'superseded'}) "
        "WHERE NOT EXISTS { (:ContextAssertion)-[:SUPERSEDES]->(p) } RETURN p.assertion_id AS id"
    ),
    "disputed_has_contradiction": (
        "MATCH (a:ContextAssertion {status: 'disputed'}) "
        "WHERE NOT EXISTS { (a)-[:CONTRADICTS]-(:ContextAssertion) } RETURN a.assertion_id AS id"
    ),
    # V3 / R9: an observed-on entity exists when the knowledge starts to hold.
    "bound_entity_exists_at_valid_from": (
        "MATCH (a:ContextAssertion)-[:ABOUT {scope: 'observed_on'}]->(e:Entity) "
        "WHERE a.valid_from < e.valid_from "
        "OR a.valid_from >= coalesce(e.valid_to, " + END_OF_TIME + ") "
        "RETURN a.assertion_id + '@' + e.entity_id AS id"
    ),
    # R6: a version can't be retracted before it was recorded.
    "retracted_after_recorded": (
        "MATCH (a:ContextAssertion) WHERE a.retracted_at IS NOT NULL "
        "AND a.retracted_at < a.recorded_at RETURN a.assertion_id AS id"
    ),
    # The access register is one non-overlapping chain per segment.
    "access_register_overlap": (
        "MATCH (r1:AccessRecord)-[:FOR]->(s:Segment)<-[:FOR]-(r2:AccessRecord) "
        "WHERE r1.record_id < r2.record_id "
        "AND r1.valid_from < coalesce(r2.valid_to, " + END_OF_TIME + ") "
        "AND r2.valid_from < coalesce(r1.valid_to, " + END_OF_TIME + ") "
        "RETURN r1.record_id + '/' + r2.record_id AS id"
    ),
}


async def run_checks(driver: AsyncDriver, database: str, limit: int = 5) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name, query in CHECKS.items():
        records, _, _ = await driver.execute_query(query, database_=database)
        ids = sorted(str(r["id"]) for r in records)
        results.append(CheckResult(name=name, violations=len(ids), examples=ids[:limit]))
    return results
