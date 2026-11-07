"""BE-1.5: ContextRepository's single write path against a real Neo4j (testcontainers).
Run: uv run pytest -m integration"""

# ruff: noqa: PT009  # `assert_` is the repository method (the card's name), not unittest

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from neo4j import AsyncDriver

from silentctx.config import Settings
from silentctx.context.models import AssertionIn
from silentctx.errors import WriteRejectedError
from silentctx.graph import schema
from silentctx.graph.driver import create_driver
from silentctx.graph.repository import ContextRepository
from tests.test_integration_neo4j import PASSWORD, neo4j_url  # noqa: F401  # shared container

pytestmark = pytest.mark.integration

T0 = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


@pytest.fixture
async def driver(
    neo4j_url: str,  # noqa: F811
    make_settings: Callable[..., Settings],
) -> AsyncIterator[AsyncDriver]:
    settings = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD)
    await schema.apply_migrations(settings)
    d = create_driver(settings)
    await d.execute_query("MATCH (n) WHERE n.test_wp = true DETACH DELETE n")
    await d.execute_query(
        "MATCH (a:ContextAssertion) WHERE a.idempotency_key IS NOT NULL DETACH DELETE a"
    )
    await d.execute_query(
        "UNWIND ['WP-SEG', 'WP-DEV'] AS id "
        "MERGE (e:Entity {entity_id: id}) SET e.test_wp = true, e.valid_from = $t0 "
        "WITH 1 AS _ UNWIND range(0, 5) AS i "
        "MERGE (m:Event:Message {event_id: 'WP-MSG-' + i}) "
        "SET m.test_wp = true, m.event_time = $t0 + duration({hours: i}), "
        "  m.author = CASE WHEN i % 2 = 0 THEN 'pekka' ELSE 'erkki' END "
        "MERGE (o:Event:Outcome {event_id: 'WP-OUT'}) SET o.test_wp = true, o.event_time = $t0",
        t0=T0,
    )
    try:
        yield d
    finally:
        await d.close()


def _item(statement: str, *msgs: int, **kw: Any) -> AssertionIn:
    return AssertionIn.model_validate(
        {
            "statement": statement,
            "topic": "site_access",
            "bindings": [{"entity_id": "WP-SEG", "scope": "observed_on"}],
            "sources": [{"event_id": f"WP-MSG-{i}", "quote": statement} for i in msgs],
        }
        | kw
    )


async def _q(driver: AsyncDriver, cypher: str, **params: Any) -> list[dict[str, Any]]:
    records, _, _ = await driver.execute_query(cypher, params)
    return [dict(r) for r in records]


async def test_assert_records_at_the_last_source_and_is_idempotent(driver: AsyncDriver) -> None:
    repo = ContextRepository(driver, "neo4j")
    out = await repo.assert_(_item("Kemi gate: padlock, key in the hut", 0, 2))
    assert out.recorded_at == T0 + timedelta(hours=2)  # R2
    assert out.valid_from == out.recorded_at  # R3 default
    assert out.valid_to is None
    assert not out.replayed

    again = await repo.assert_(_item("Kemi gate:  padlock, key in the HUT", 2, 0))
    assert (again.assertion_id, again.replayed) == (out.assertion_id, True)

    rows = await _q(
        driver,
        "MATCH (a:ContextAssertion {assertion_id: $id}) "
        "RETURN a.asserted_by AS by, "
        "[(a)-[b:ABOUT]->(e) | e.entity_id + '/' + b.scope] AS about, "
        "[(a)-[:DERIVED_FROM]->(ev)-[:AUTHORED_BY]->(p:Person) | p.person_id] AS authors",
        id=out.assertion_id,
    )
    assert rows == [{"by": "pekka", "about": ["WP-SEG/observed_on"], "authors": ["pekka"] * 2}]


async def test_unknown_entity_or_event_writes_nothing(driver: AsyncDriver) -> None:
    repo = ContextRepository(driver, "neo4j")
    bad_entity = _item("x", 1, bindings=[{"entity_id": "WP-NOPE", "scope": "observed_on"}])
    with pytest.raises(WriteRejectedError, match="unknown entities: WP-NOPE") as exc:
        await repo.assert_(bad_entity)
    assert exc.value.status == 422
    with pytest.raises(WriteRejectedError, match="unknown source events"):
        await repo.assert_(_item("x", 1, sources=[{"event_id": "WP-MSG-99"}]))
    assert await _q(driver, "MATCH (e:Entity {entity_id: 'WP-NOPE'}) RETURN e") == []  # ST-12
    assert await _q(driver, "MATCH (a:ContextAssertion {statement: 'x'}) RETURN a") == []


async def test_supersede_closes_the_predecessor_at_the_successor_start(
    driver: AsyncDriver,
) -> None:
    repo = ContextRepository(driver, "neo4j")
    pad = await repo.assert_(_item("padlock", 0))
    with pytest.raises(WriteRejectedError, match="must start after"):
        await repo.supersede(pad.assertion_id, _item("too early", 1, valid_from=T0.isoformat()))
    # Rolled back: the rejected successor doesn't exist.
    assert await _q(driver, "MATCH (a:ContextAssertion {statement: 'too early'}) RETURN a") == []

    start = T0 + timedelta(days=7)
    code = await repo.supersede(pad.assertion_id, _item("code lock", 3, valid_from=start))
    [view] = await repo.assertions([pad.assertion_id])
    assert (view.valid_to, view.status, view.superseded_by) == (
        start,
        "superseded",
        code.assertion_id,
    )
    with pytest.raises(WriteRejectedError, match="already superseded"):
        await repo.supersede(pad.assertion_id, _item("third", 4, valid_from=start))


async def test_correct_retracts_the_wrong_version_and_keeps_its_window(
    driver: AsyncDriver,
) -> None:
    repo = ContextRepository(driver, "neo4j")
    wrong = await repo.assert_(_item("gate code 1234", 1, valid_from=T0))
    fixed = await repo.correct(wrong.assertion_id, _item("gate code 4321", 1), "WP-MSG-5")
    assert fixed.valid_from == T0  # R6 ii: keeps the original start
    assert fixed.recorded_at == T0 + timedelta(hours=5)  # recorded at the correction
    rows = await _q(
        driver,
        "MATCH (w:ContextAssertion {assertion_id: $w}), (f:ContextAssertion {assertion_id: $f}) "
        "RETURN w.retracted_at AS at, EXISTS { (f)-[:SUPERSEDES]-(w) } AS sup, "
        "EXISTS { (f)-[:CORRECTS]->(w) } AS corr",
        w=wrong.assertion_id,
        f=fixed.assertion_id,
    )
    assert rows[0]["at"].to_native() == T0 + timedelta(hours=5)
    assert (rows[0]["sup"], rows[0]["corr"]) == (False, True)  # ST-05
    with pytest.raises(WriteRejectedError, match="already retracted"):
        await repo.correct(wrong.assertion_id, _item("again", 1), "WP-MSG-5")


async def test_retract_and_evidence(driver: AsyncDriver) -> None:
    repo = ContextRepository(driver, "neo4j")
    a = await repo.assert_(_item("bounce fixes the stall", 2))
    with pytest.raises(WriteRejectedError, match="retracted_at can't be before recorded_at"):
        await repo.retract(a.assertion_id, T0)
    at = T0 + timedelta(days=1)
    assert (await repo.retract(a.assertion_id, at)).retracted_at == at
    assert (await repo.retract(a.assertion_id, at)).replayed  # same instant: no-op

    b = await repo.assert_(_item("bounce works on Gen1", 3))
    for _ in range(2):  # the same outcome counts once
        counters = await repo.add_evidence("WP-OUT", b.assertion_id, "SUPPORTS", True)
    assert (counters["support_n"], counters["refute_n"]) == (1, 0)
    with pytest.raises(WriteRejectedError):
        await repo.add_evidence("WP-NOPE", b.assertion_id, "REFUTES")


async def test_contradiction_disputes_both_sides(driver: AsyncDriver) -> None:
    repo = ContextRepository(driver, "neo4j")
    old = await repo.assert_(_item("reboot fixes it", 1, status="believed_true"))
    new = await repo.assert_(_item("reboot makes it worse", 4, contradicts=[old.assertion_id]))
    views = {v.assertion_id: v for v in await repo.assertions([old.assertion_id, new.assertion_id])}
    assert {v.status for v in views.values()} == {"disputed"}
    assert views[old.assertion_id].contradicts == [new.assertion_id]


async def test_correcting_a_successor_hands_the_supersession_to_the_fix(
    driver: AsyncDriver,
) -> None:
    repo = ContextRepository(driver, "neo4j")
    start = T0 + timedelta(days=1)
    pad = await repo.assert_(_item("padlock", 0))
    wrong = await repo.supersede(pad.assertion_id, _item("code lock 1111", 2, valid_from=start))
    fixed = await repo.correct(wrong.assertion_id, _item("code lock 2222", 2), "WP-MSG-4")
    assert fixed.valid_from == start

    [view] = await repo.assertions([pad.assertion_id])
    assert (view.superseded_by, view.valid_to) == (fixed.assertion_id, start)
    [fview] = await repo.assertions([fixed.assertion_id])
    assert fview.supersedes == pad.assertion_id

    # R10: the padlock was known to be superseded from the first (wrong) version on.
    later = T0 + timedelta(days=2)
    for hours, in_scope in ((1, True), (3, False), (5, False)):
        found = await repo.assertions([pad.assertion_id], later, T0 + timedelta(hours=hours))
        assert bool(found) is in_scope, hours


async def test_correcting_a_superseded_version_keeps_it_superseded(driver: AsyncDriver) -> None:
    repo = ContextRepository(driver, "neo4j")
    start = T0 + timedelta(days=1)
    wrong = await repo.assert_(_item("padlock, key at Kemi depot", 0))
    code = await repo.supersede(wrong.assertion_id, _item("code lock", 3, valid_from=start))
    fixed = await repo.correct(wrong.assertion_id, _item("padlock, key at Tornio", 0), "WP-MSG-5")
    assert (fixed.status, fixed.valid_from, fixed.valid_to) == ("superseded", T0, start)

    [fview] = await repo.assertions([fixed.assertion_id])
    assert fview.superseded_by == code.assertion_id
    [cview] = await repo.assertions([code.assertion_id])
    assert cview.supersedes == fixed.assertion_id
    with pytest.raises(WriteRejectedError, match="already superseded"):
        await repo.supersede(fixed.assertion_id, _item("third", 4, valid_from=start))
