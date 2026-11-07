"""graph/checks.cypher against a real Neo4j (testcontainers): the seeded graph is clean, each
rule finds the violation it is about, a write that breaks one rolls back (V5), and every
ingest runs the event checks. Run: uv run pytest -m integration"""

# ruff: noqa: PT009  # `assert_` is the repository method, not unittest

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from neo4j import AsyncDriver
from structlog.testing import capture_logs
from typer.testing import CliRunner

from kaiku.config import Settings
from kaiku.context.models import AssertionIn
from kaiku.domain.models import Platform
from kaiku.errors import WriteRejectedError
from kaiku.graph import schema
from kaiku.graph.checks import CHECKS, CheckResult, run_checks
from kaiku.graph.driver import create_driver
from kaiku.graph.repository import ContextRepository
from kaiku.ingest.backfill import backfill
from kaiku.ingest.store import CaptureStore, MessageIn
from kaiku.seed.profiles import Profile
from kaiku.seed.service import SeedRequest, seed
from kaiku.seed.sources import MockData
from tests.capture_helpers import ELISA_DIR, MOCK_DATA
from tests.test_integration_neo4j import PASSWORD, neo4j_url  # noqa: F401  # shared container

pytestmark = pytest.mark.integration

T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 7, tzinfo=UTC)  # the corpus snapshot
_CLEAN = (
    "MATCH (n) WHERE n.chk = true OR n.event_id STARTS WITH 'chk:' "
    "OR n.thread_id STARTS WITH 'chk:' DETACH DELETE n"
)


@pytest.fixture
def settings(
    neo4j_url: str,  # noqa: F811
    make_settings: Callable[..., Settings],
) -> Settings:
    return make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA)


@pytest.fixture
async def driver(settings: Settings) -> AsyncIterator[AsyncDriver]:
    await schema.apply_migrations(settings)
    d = create_driver(settings)
    await d.execute_query(_CLEAN)
    try:
        yield d
    finally:
        await d.execute_query(_CLEAN)
        await d.close()


def _by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {r.name: r for r in results}


async def _found(driver: AsyncDriver, **kw: Any) -> dict[str, list[str]]:
    """Every check's violations that involve this test's nodes (ids start with CHK)."""
    results = await run_checks(driver, "neo4j", limit=10_000, now=NOW, **kw)
    return {
        r.name: [x for x in r.examples if "CHK" in x or "chk:" in x]
        for r in results
        if any("CHK" in x or "chk:" in x for x in r.examples)
    }


# --- Seeded data is clean -----------------------------------------------------------------------


def test_seeded_and_backfilled_graph_has_no_violations(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The network, the gold layer and the full backfill (Teams, .msg, Elisa's example)
    satisfy every check, and `kaiku eval time` says so with exit code 0."""

    async def go() -> list[CheckResult]:
        await seed(settings, SeedRequest(network=True, gold=True, profile=Profile.FULL))
        d = create_driver(settings)
        try:
            store = CaptureStore(d, settings.neo4j_database)
            report = await backfill(
                store, MockData(MOCK_DATA), Profile.FULL, elisa_dir=ELISA_DIR, now=NOW
            )
            assert report.new_events >= 0
            counts, _, _ = await d.execute_query(
                "MATCH (m:Message)-[:IN_THREAD]->(t:Thread) "
                "RETURN count(DISTINCT m) AS m, count(DISTINCT t) AS t"
            )
            assert counts[0]["m"] > 700
            return await run_checks(d, settings.neo4j_database)
        finally:
            await d.close()

    results = asyncio.run(go())
    assert {r.name for r in results} == set(CHECKS)
    assert [r for r in results if r.violations] == []

    from kaiku.cli import app

    monkeypatch.setattr("kaiku.cli.get_settings", lambda: settings)
    out = CliRunner().invoke(app, ["eval", "time"])
    assert out.exit_code == 0, out.output
    assert "FAIL" not in out.output
    assert "ok   thread_strictly_increasing: 0" in out.output


# --- Each rule finds its violation ---------------------------------------------------------------

# One fixture graph with a violation of every rule, next to the same shape done right.
_BROKEN = """
CREATE (dev:Entity:Device {entity_id: 'CHK-DEV', valid_from: $t0, chk: true})
CREATE (late:Entity:Device {entity_id: 'CHK-LATE', valid_from: $t0 + duration('P30D'), chk: true})
CREATE (seg:Entity:Segment {entity_id: 'CHK-SEG', valid_from: $t0, chk: true})
WITH dev, late, seg
CREATE (e1:Event:Message {event_id: 'chk:e1', event_time: $t0, chk: true})
CREATE (e2:Event:Message {event_id: 'chk:e2', event_time: $t0 + duration('PT1H'), chk: true})
// R1: no event_time, a string event_time, a record time that differs, a validity window
CREATE (:Event {event_id: 'chk:no-time', chk: true})
CREATE (:Event {event_id: 'chk:text-time', event_time: '2026-03-02T08:00:00Z', chk: true})
CREATE (:Event {event_id: 'chk:record-time', event_time: $t0,
  record_time: $t0 + duration('PT1M'), chk: true})
CREATE (:Event {event_id: 'chk:valid', event_time: $t0, valid_from: $t0, chk: true})
CREATE (:Event:DeviceEvent {event_id: 'chk:ends-early', event_time: $t0, end_time: $t0,
  chk: true})
// R7: a tie without a platform sequence breaks the order; a tie with one does not
CREATE (t1:Thread {thread_id: 'chk:tied', chk: true})
CREATE (:Event:Message {event_id: 'chk:t1a', event_time: $t0, chk: true})-[:IN_THREAD]->(t1)
CREATE (:Event:Message {event_id: 'chk:t1b', event_time: $t0, chk: true})-[:IN_THREAD]->(t1)
CREATE (t2:Thread {thread_id: 'chk:ordered', chk: true})
CREATE (:Event:Message {event_id: 'chk:t2a', event_time: $t0, platform_seq: '01',
  chk: true})-[:IN_THREAD]->(t2)
CREATE (:Event:Message {event_id: 'chk:t2b', event_time: $t0, platform_seq: '02',
  chk: true})-[:IN_THREAD]->(t2)
// Windows and status
CREATE (:ContextAssertion {assertion_id: 'CHK-EMPTY', status: 'believed_true',
  valid_from: $t0, valid_to: $t0, recorded_at: $t0, chk: true})
CREATE (:ContextAssertion {assertion_id: 'CHK-OPEN-ENDED', status: 'believed_true',
  valid_from: $t0, valid_to: $t0 + duration('P1D'), recorded_at: $t0, chk: true})
CREATE (:ContextAssertion {assertion_id: 'CHK-STATED-END', status: 'believed_true',
  valid_from: $t0, valid_to: $now + duration('P10D'), recorded_at: $t0, chk: true})
CREATE (:ContextAssertion {assertion_id: 'CHK-EXPIRED-FUTURE', status: 'expired',
  valid_from: $t0, valid_to: $now + duration('P10D'), recorded_at: $t0, chk: true})
// Supersession: a wrong boundary, no successor, an expired one with a successor
CREATE (p1:ContextAssertion {assertion_id: 'CHK-P1', status: 'superseded', valid_from: $t0,
  valid_to: $t0 + duration('P2D'), recorded_at: $t0, chk: true})
CREATE (s1:ContextAssertion {assertion_id: 'CHK-S1', status: 'believed_true',
  valid_from: $t0 + duration('P3D'), recorded_at: $t0, chk: true})
CREATE (s1)-[:SUPERSEDES]->(p1)
CREATE (:ContextAssertion {assertion_id: 'CHK-ORPHAN', status: 'superseded', valid_from: $t0,
  valid_to: $t0 + duration('P2D'), recorded_at: $t0, chk: true})
CREATE (p2:ContextAssertion {assertion_id: 'CHK-EXPIRED', status: 'expired', valid_from: $t0,
  valid_to: $t0 + duration('P2D'), recorded_at: $t0, chk: true})
CREATE (s2:ContextAssertion {assertion_id: 'CHK-S2', status: 'believed_true',
  valid_from: $t0 + duration('P2D'), recorded_at: $t0, chk: true})
CREATE (s2)-[:SUPERSEDES]->(p2)
// A predecessor handed back by a retracted successor keeps its old valid_to: not a violation
CREATE (p3:ContextAssertion {assertion_id: 'CHK-REOPENED', status: 'believed_true',
  valid_from: $t0, valid_to: $t0 + duration('P2D'), recorded_at: $t0, chk: true})
CREATE (s3:ContextAssertion {assertion_id: 'CHK-RETRACTED', status: 'believed_true',
  valid_from: $t0 + duration('P2D'), recorded_at: $t0,
  retracted_at: $t0 + duration('P5D'), chk: true})
CREATE (s3)-[:SUPERSEDES]->(p3)
// Asymmetric links: superseded_by without the edge, a supersession both ways, a symmetric
// relation stored both ways
CREATE (:ContextAssertion {assertion_id: 'CHK-MIRROR', status: 'superseded', valid_from: $t0,
  valid_to: $t0 + duration('P2D'), recorded_at: $t0, superseded_by: 'CHK-S1', chk: true})
CREATE (c1:ContextAssertion {assertion_id: 'CHK-C1', status: 'believed_true',
  valid_from: $t0, recorded_at: $t0, chk: true})
CREATE (c2:ContextAssertion {assertion_id: 'CHK-C2', status: 'believed_true',
  valid_from: $t0, recorded_at: $t0, chk: true})
CREATE (c1)-[:SUPERSEDES]->(c2) CREATE (c2)-[:SUPERSEDES]->(c1)
CREATE (c1)-[:CORROBORATES]->(c2) CREATE (c2)-[:CORROBORATES]->(c1)
CREATE (:ContextAssertion {assertion_id: 'CHK-DISPUTED', status: 'disputed',
  valid_from: $t0, recorded_at: $t0, chk: true})
// Overlapping access register
CREATE (:AccessRecord {record_id: 'CHK-R1', valid_from: $t0, chk: true})-[:FOR]->(seg)
CREATE (:AccessRecord {record_id: 'CHK-R2', valid_from: $t0 + duration('P1D'),
  chk: true})-[:FOR]->(seg)
// The subject must exist at valid_from, unless the binding is a V3 candidate
CREATE (b1:ContextAssertion {assertion_id: 'CHK-BEFORE', status: 'believed_true',
  valid_from: $t0, recorded_at: $t0, chk: true})
CREATE (b1)-[:ABOUT {scope: 'observed_on'}]->(late)
CREATE (b2:ContextAssertion {assertion_id: 'CHK-CANDIDATE', status: 'believed_true',
  valid_from: $t0, recorded_at: $t0, chk: true})
CREATE (b2)-[:ABOUT {scope: 'observed_on', candidate: true}]->(late)
// R2: recorded at the first source instead of the last; a proposal is recorded when proposed
CREATE (r1:ContextAssertion {assertion_id: 'CHK-EARLY-RECORD', origin: 'extracted',
  status: 'believed_true', valid_from: $t0, recorded_at: $t0, chk: true})
CREATE (r1)-[:DERIVED_FROM]->(e1) CREATE (r1)-[:DERIVED_FROM]->(e2)
CREATE (r2:ContextAssertion {assertion_id: 'CHK-RECORDED', origin: 'extracted',
  status: 'believed_true', valid_from: $t0, recorded_at: $t0 + duration('PT1H'), chk: true})
CREATE (r2)-[:DERIVED_FROM]->(e1) CREATE (r2)-[:DERIVED_FROM]->(e2)
CREATE (r3:ContextAssertion {assertion_id: 'CHK-PROPOSED', origin: 'agent_proposal',
  status: 'unverified', valid_from: $t0, recorded_at: $t0 + duration('P1D'), chk: true})
CREATE (r3)-[:DERIVED_FROM]->(e2)
CREATE (r4:ContextAssertion {assertion_id: 'CHK-PROPOSED-EARLY', origin: 'agent_proposal',
  status: 'unverified', valid_from: $t0, recorded_at: $t0, chk: true})
CREATE (r4)-[:DERIVED_FROM]->(e2)
"""

EXPECTED = {
    "node_window_order": ["CHK-EMPTY"],
    "open_status_past_valid_to": ["CHK-EMPTY", "CHK-OPEN-ENDED"],
    "expired_has_ended": ["CHK-EXPIRED-FUTURE"],
    "supersession_timestamp": ["CHK-C1>CHK-C2", "CHK-C2>CHK-C1", "CHK-S1>CHK-P1"],
    "superseded_has_successor": ["CHK-MIRROR", "CHK-ORPHAN"],
    "expired_has_no_successor": ["CHK-EXPIRED"],
    "superseded_by_mirrors_edge": ["CHK-MIRROR<CHK-S1"],
    "supersedes_is_asymmetric": ["CHK-C1>CHK-C2", "CHK-C2>CHK-C1"],
    "symmetric_link_stored_once": ["CORROBORATES:CHK-C1/CHK-C2"],
    "disputed_has_contradiction": ["CHK-DISPUTED"],
    "access_register_overlap": ["CHK-R1/CHK-R2"],
    "bound_entity_exists_at_valid_from": ["CHK-BEFORE@CHK-LATE"],
    "recorded_at_is_source_event_time": ["CHK-EARLY-RECORD", "CHK-PROPOSED-EARLY"],
    "event_has_one_event_time": ["chk:no-time", "chk:record-time", "chk:text-time"],
    "event_has_no_validity": ["chk:valid"],
    "event_ends_after_it_starts": ["chk:ends-early"],
    "thread_strictly_increasing": ["chk:tied: chk:t1a / chk:t1b"],
}


async def test_each_rule_finds_its_violation_and_nothing_else(driver: AsyncDriver) -> None:
    await driver.execute_query(_BROKEN, t0=T0, now=NOW)
    assert await _found(driver) == EXPECTED


async def test_a_scope_runs_only_the_checks_anchored_on_it(driver: AsyncDriver) -> None:
    await driver.execute_query(_BROKEN, t0=T0, now=NOW)
    found = await _found(driver, scope={"assertion": ["CHK-EARLY-RECORD", "CHK-C2"]})
    assert found == {
        "recorded_at_is_source_event_time": ["CHK-EARLY-RECORD"],
        "supersession_timestamp": ["CHK-C1>CHK-C2", "CHK-C2>CHK-C1"],
        "supersedes_is_asymmetric": ["CHK-C1>CHK-C2", "CHK-C2>CHK-C1"],
        "symmetric_link_stored_once": ["CORROBORATES:CHK-C1/CHK-C2"],
    }
    found = await _found(driver, scope={"event": ["chk:valid"], "thread": ["chk:ordered"]})
    assert found == {"event_has_no_validity": ["chk:valid"]}


def test_eval_time_fails_on_a_violation(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def go(query: str, **params: Any) -> None:
        d = create_driver(settings)
        try:
            await schema.apply_migrations(settings)
            await d.execute_query(query, params)
        finally:
            await d.close()

    asyncio.run(go(_CLEAN))
    asyncio.run(go(_BROKEN, t0=T0, now=NOW))
    from kaiku.cli import app

    monkeypatch.setattr("kaiku.cli.get_settings", lambda: settings)
    try:
        out = CliRunner().invoke(app, ["eval", "time", "--at", "2026-11-07T00:00:00Z"])
        assert out.exit_code == 1, out.output
        assert "FAIL thread_strictly_increasing: 1 chk:tied" in out.output
        bad = CliRunner().invoke(app, ["eval", "time", "--at", "yesterday"])
        assert bad.exit_code == 2
    finally:
        asyncio.run(go(_CLEAN))


# --- Inside every write (V5) ---------------------------------------------------------------------


async def test_a_write_that_breaks_a_rule_rolls_back(driver: AsyncDriver) -> None:
    """The new assertion would contradict one whose CONTRADICTS link points at itself: the
    checks see it in the write's own transaction, and nothing is written."""
    await driver.execute_query(
        "CREATE (:Entity:Segment {entity_id: 'CHK-SEG', valid_from: $t0, chk: true}) "
        "CREATE (:Event:Message {event_id: 'chk:src', event_time: $t0 + duration('PT1H'), "
        "  author: 'erkki', chk: true}) "
        "CREATE (x:ContextAssertion {assertion_id: 'CHK-SELF', status: 'believed_true', "
        "  valid_from: $t0, recorded_at: $t0, chk: true}) "
        "CREATE (x)-[:CONTRADICTS]->(x)",
        t0=T0,
    )
    repo = ContextRepository(driver, "neo4j")
    item = AssertionIn.model_validate(
        {
            "statement": "CHK gate: code lock 4321",
            "bindings": [{"entity_id": "CHK-SEG", "scope": "observed_on"}],
            "sources": [{"event_id": "chk:src", "quote": "code lock 4321"}],
            "contradicts": ["CHK-SELF"],
        }
    )
    with pytest.raises(WriteRejectedError, match="symmetric_link_stored_once"):
        await repo.assert_(item)
    records, _, _ = await driver.execute_query(
        "MATCH (a:ContextAssertion {statement: 'CHK gate: code lock 4321'}) RETURN count(a) AS n"
    )
    assert records[0]["n"] == 0

    # Without the broken neighbour the same write goes through and passes the checks.
    await driver.execute_query(
        "MATCH (:ContextAssertion {assertion_id: 'CHK-SELF'})-[r:CONTRADICTS]->() DELETE r"
    )
    out = await repo.assert_(item.model_copy(update={"contradicts": []}))
    assert out.recorded_at == T0 + timedelta(hours=1)
    await driver.execute_query(
        "MATCH (a:ContextAssertion {assertion_id: $id}) SET a.chk = true", id=out.assertion_id
    )


# --- After every ingest ---------------------------------------------------------------------------


def _message(event_id: str, at: datetime, seq: str | None = None) -> MessageIn:
    return MessageIn(
        event_id=event_id,
        platform=Platform.SLACK,
        channel="CCHK",
        thread_id="chk:thread",
        event_time=at,
        platform_seq=seq,
        text=event_id,
    )


async def test_ingest_runs_the_event_and_thread_checks(driver: AsyncDriver) -> None:
    store = CaptureStore(driver, "neo4j")
    with capture_logs() as logs:
        assert await store.store([_message("chk:m1", T0, "1"), _message("chk:m2", T0, "2")], NOW)
    assert [e for e in logs if e["event"] == "temporal_violation"] == []

    with capture_logs() as logs:
        assert await store.store([_message("chk:m3", T0 + timedelta(minutes=1))], NOW) == 1
        assert await store.store([_message("chk:m4", T0 + timedelta(minutes=1))], NOW) == 1
    warnings = [e for e in logs if e["event"] == "temporal_violation"]
    assert [(w["check"], w["ids"]) for w in warnings] == [
        ("thread_strictly_increasing", ["chk:thread: chk:m3 / chk:m4"])
    ]
    failed = await store.check([_message("chk:m4", T0)])
    assert [r.name for r in failed] == ["thread_strictly_increasing"]
