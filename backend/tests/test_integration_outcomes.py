"""The outcome verifier against a real Neo4j (plan 001 §6.2): the network confirms the
knowledge, or counts against it only when its conditions held. Run: uv run pytest -m integration
"""

# ruff: noqa: PT009  # `assert_` is the repository method, not unittest

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from neo4j import AsyncDriver

from kaiku.config import Settings
from kaiku.context.models import AssertionIn
from kaiku.domain.time import PM_INTERVAL
from kaiku.graph import schema
from kaiku.graph.driver import create_driver
from kaiku.graph.repository import ContextRepository
from kaiku.telemetry.store import LivePoint, Sample, TelemetryStore
from kaiku.validation.outcomes import OutcomeVerifier
from tests.test_integration_neo4j import PASSWORD, neo4j_url  # noqa: F401  # shared container

pytestmark = pytest.mark.integration

NODE, PORT = "VO-DEV", "1/1/2"
T_ACTION = datetime(2026, 11, 7, 10, 5, tzinfo=UTC)  # between PM intervals
STALL = ["oper_up", "rx_power_normal", "counters_zero"]


@pytest.fixture
async def driver(
    neo4j_url: str,  # noqa: F811
    make_settings: Callable[..., Settings],
) -> AsyncIterator[AsyncDriver]:
    settings = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD)
    await schema.apply_migrations(settings)
    d = create_driver(settings)
    await d.execute_query("MATCH (n) WHERE n.test_vo = true DETACH DELETE n")
    await d.execute_query(
        "MATCH (a:ContextAssertion)-[:DERIVED_FROM]->(:Event {test_vo: true}) DETACH DELETE a"
    )
    await d.execute_query("MATCH (o:Outcome)-[:ON]->(:Entity {test_vo: true}) DETACH DELETE o")
    await d.execute_query(
        "UNWIND ['VO-DEV', 'VO-MODEL'] AS id "
        "MERGE (e:Entity {entity_id: id}) SET e.test_vo = true, e.valid_from = $t0 "
        "MERGE (m:Event:Message {event_id: 'VO-MSG'}) "
        "SET m.test_vo = true, m.event_time = $t0, m.author = 'erkki'",
        t0=T_ACTION - timedelta(days=30),
    )
    try:
        yield d
    finally:
        await d.close()


async def _q(driver: AsyncDriver, cypher: str, **params: Any) -> list[dict[str, Any]]:
    records, _, _ = await driver.execute_query(cypher, params)
    return [dict(r) for r in records]


async def _rule(repo: ContextRepository, statement: str) -> str:
    """An extracted rule, unverified, scoped to the model (not to the device it will run on)."""
    out = await repo.assert_(
        AssertionIn.model_validate(
            {
                "statement": statement,
                "topic": "forwarding_state_after_sfp_swap",
                "qualifiers": {"symptom": STALL, "action": "bounce_interface"},
                "bindings": [{"entity_id": "VO-MODEL", "scope": "applies_to"}],
                "sources": [{"event_id": "VO-MSG", "quote": statement}],
            }
        )
    )
    assert out.status == "unverified"
    return out.assertion_id


def _telemetry(*, stalled_before: bool, recovers: bool) -> TelemetryStore:
    """PM samples around the action: the hour before (a stall, or traffic flowing), then
    the intervals after it (traffic back, or still zero)."""

    def sample(ts: datetime, mbps: float) -> Sample:
        return Sample(
            ts=ts,
            ne_id=NODE,
            port=PORT,
            in_octets_mbps=mbps,
            out_octets_mbps=mbps,
            rx_power_dbm=-4.0,
            oper_status="UP",
        )

    start = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    before = [sample(start + i * PM_INTERVAL, 0.0 if stalled_before else 1.8) for i in range(5)]
    after = [sample(start + i * PM_INTERVAL, 1.8 if recovers else 0.0) for i in range(5, 8)]
    store = TelemetryStore(None)
    store.add_live(before + after)
    return store


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _outcome(
    repo: ContextRepository, aid: str, result: str, key: str, met: bool | None = None
) -> str:
    out, _ = await repo.record_outcome(
        agent_id="ops-agent",
        idempotency_key=key,
        now=T_ACTION,
        entity_id=NODE,
        assertion_id=aid,
        port=PORT,
        action="bounce_interface",
        result=result,
        observed_at=T_ACTION,
        evidence_ref=None,
        conditions_met=met,
    )
    return str(out["event_id"])


async def test_a_network_confirmed_success_believes_the_rule_with_no_click(
    driver: AsyncDriver,
) -> None:
    """Resolved -> SUPPORTS, the device joins observed_on, unverified ->
    believed_true, and confidence is the Beta mean. Conditions are filled in from the
    telemetry before the action."""
    repo = ContextRepository(driver, "neo4j")
    aid = await _rule(repo, "bounce the port when counters are zero after an SFP swap")
    oid = await _outcome(repo, aid, "resolved", "vo-ok")
    verifier = OutcomeVerifier(
        repo,
        _telemetry(stalled_before=True, recovers=True),
        clock=_Clock(T_ACTION + timedelta(minutes=40)),
    )
    state = await verifier.verify(oid)
    assert state["verification"] == "resolved"
    assert (state["evidence"], state["support_n"], state["refute_n"]) == ("SUPPORTS", 1, 0)
    assert state["status"] == "believed_true"
    assert state["conditions_met"] is True  # recorded: the stall was there before the action
    assert state["confidence"] == pytest.approx(2 / 3)

    [view] = await repo.assertions([aid])
    assert view.network_confirmed_on == [NODE]
    assert {(b.entity_id, b.scope) for b in view.bindings} == {
        ("VO-MODEL", "applies_to"),
        (NODE, "observed_on"),
    }
    assert view.confidence == pytest.approx(2 / 3)
    [edge] = await _q(
        driver,
        "MATCH (:Outcome {event_id: $o})-[r:SUPPORTS]->(:ContextAssertion) "
        "RETURN r.verified AS verified",
        o=oid,
    )
    assert edge == {"verified": True}

    # Final: verifying again counts nothing twice.
    again = await verifier.verify(oid)
    assert (again["support_n"], again["verification"]) == (1, "resolved")
    *_, evidence, _ = await repo.history(aid)
    assert [(e.relation, e.verified, e.overridden) for e in evidence] == [("SUPPORTS", True, False)]


async def test_a_failure_where_the_rule_applied_refutes_it_and_asks_for_review(
    driver: AsyncDriver,
) -> None:
    repo = ContextRepository(driver, "neo4j")
    aid = await _rule(repo, "bounce clears the stall (refuted)")
    oid = await _outcome(repo, aid, "unknown", "vo-fail")  # the reporter couldn't tell
    clock = _Clock(T_ACTION + timedelta(minutes=10))
    verifier = OutcomeVerifier(repo, _telemetry(stalled_before=True, recovers=False), clock=clock)
    assert (await verifier.verify(oid))["verification"] == "pending"  # window still open
    assert oid in await repo.outcomes_to_verify()

    clock.now = T_ACTION + timedelta(minutes=31)
    assert await verifier.sweep() >= 1  # the api's periodic job
    state = await repo.outcome_state(oid)
    assert state["verification"] == "not_resolved"
    assert (state["evidence"], state["support_n"], state["refute_n"]) == ("REFUTES", 0, 1)
    assert state["review_due"] is True
    assert state["status"] == "unverified"  # doubt never confirms (R5: validity untouched)
    assert state["confidence"] == pytest.approx(1 / 3)
    assert oid not in await repo.outcomes_to_verify()


async def test_a_failure_where_the_conditions_were_not_met_is_not_counted(
    driver: AsyncDriver,
) -> None:
    """The counters stayed at zero, but the conditions were not met before the
    action (as report_outcome recorded them), so the rule didn't apply."""
    repo = ContextRepository(driver, "neo4j")
    aid = await _rule(repo, "bounce clears the stall (did not apply)")
    oid = await _outcome(repo, aid, "not_resolved", "vo-na", met=False)
    verifier = OutcomeVerifier(
        repo,
        _telemetry(stalled_before=True, recovers=False),
        clock=_Clock(T_ACTION + timedelta(hours=1)),
    )
    state = await verifier.verify(oid)
    assert state["verification"] == "not_resolved"
    assert state["conditions_met"] is False
    assert (state["evidence"], state["support_n"], state["refute_n"]) == (None, 0, 0)
    assert state["review_due"] is False
    assert state["status"] == "unverified"


async def test_traffic_that_never_stalled_confirms_nothing(driver: AsyncDriver) -> None:
    """Traffic was flowing before the action and after it: the network can't credit the
    advice, so nothing is counted and the rule stays unverified."""
    repo = ContextRepository(driver, "neo4j")
    aid = await _rule(repo, "bounce clears the stall (never stalled)")
    oid = await _outcome(repo, aid, "unknown", "vo-flowing")
    verifier = OutcomeVerifier(
        repo,
        _telemetry(stalled_before=False, recovers=True),
        clock=_Clock(T_ACTION + timedelta(hours=1)),
    )
    state = await verifier.verify(oid)
    assert state["verification"] == "resolved"
    assert state["conditions_met"] is False  # filled in from the telemetry before the action
    assert (state["evidence"], state["support_n"], state["status"]) == (None, 0, "unverified")
    [view] = await repo.assertions([aid])
    assert view.network_confirmed_on == []
    assert NODE not in {b.entity_id for b in view.bindings}


async def test_the_network_overrides_a_report_it_disagrees_with(driver: AsyncDriver) -> None:
    """Someone clicked "done ✓", but the counters stayed at zero: the reported SUPPORTS is
    kept on record and no longer counted; the network's REFUTES is."""
    repo = ContextRepository(driver, "neo4j")
    aid = await _rule(repo, "bounce clears the stall (overridden)")
    oid = await _outcome(repo, aid, "resolved", "vo-claim", met=True)
    assert (await repo.outcome_state(oid))["support_n"] == 1  # the report counts at once
    verifier = OutcomeVerifier(
        repo,
        _telemetry(stalled_before=True, recovers=False),
        clock=_Clock(T_ACTION + timedelta(hours=1)),
    )
    state = await verifier.verify(oid)
    assert (state["evidence"], state["support_n"], state["refute_n"]) == ("REFUTES", 0, 1)
    assert state["status"] == "unverified"
    *_, evidence, _ = await repo.history(aid)
    assert sorted((e.relation, e.verified, e.overridden) for e in evidence) == [
        ("REFUTES", True, False),
        ("SUPPORTS", False, True),
    ]
    [view] = await repo.assertions([aid])
    assert view.network_confirmed_on == []


async def test_no_telemetry_leaves_the_report_standing(driver: AsyncDriver) -> None:
    repo = ContextRepository(driver, "neo4j")
    aid = await _rule(repo, "bounce clears the stall (no data)")
    oid = await _outcome(repo, aid, "resolved", "vo-nodata", met=True)
    verifier = OutcomeVerifier(
        repo, TelemetryStore(None), clock=_Clock(T_ACTION + timedelta(hours=1))
    )
    state = await verifier.verify(oid)
    assert state["verification"] == "unknown"
    assert (state["evidence"], state["support_n"]) == ("SUPPORTS", 1)
    assert state["status"] == "unverified"  # only the network (or a person) confirms


async def test_the_fine_live_series_confirms_a_bounce_within_seconds(
    driver: AsyncDriver,
) -> None:
    """The live demo: PM samples won't show the bounce for 15+ minutes; the simulator's
    fine series shows traffic back half a minute after it."""

    class Live(TelemetryStore):
        async def live_points(self, ne_id: str, port: str, since: datetime) -> list[LivePoint]:
            assert (ne_id, port, since) == (NODE, PORT, T_ACTION)
            return [
                LivePoint(ts=T_ACTION + timedelta(seconds=s), oper_status=o, in_octets_mbps=m,
                          rx_power_dbm=-4.0)
                for s, o, m in ((5, "DOWN", 0.0), (25, "UP", 0.0), (35, "UP", 1.7))
            ]  # fmt: skip

    repo = ContextRepository(driver, "neo4j")
    aid = await _rule(repo, "bounce clears the stall (live)")
    oid = await _outcome(repo, aid, "resolved", "vo-live", met=True)
    verifier = OutcomeVerifier(repo, Live(None), clock=_Clock(T_ACTION + timedelta(seconds=40)))
    state = await verifier.verify(oid)
    assert (state["verification"], state["status"], state["support_n"]) == (
        "resolved",
        "believed_true",
        1,
    )


async def test_a_device_newer_than_the_knowledge_is_confirmed_but_not_bound(
    driver: AsyncDriver,
) -> None:
    """A bound entity must exist when the knowledge starts to hold: a device commissioned
    after it still counts as support, and shows as network-confirmed, without a binding."""
    repo = ContextRepository(driver, "neo4j")
    aid = await _rule(repo, "bounce clears the stall (new device)")
    await driver.execute_query(
        "MATCH (e:Entity {entity_id: $id}) SET e.valid_from = $t", id=NODE, t=T_ACTION
    )
    oid = await _outcome(repo, aid, "resolved", "vo-new-device", met=True)
    verifier = OutcomeVerifier(
        repo,
        _telemetry(stalled_before=True, recovers=True),
        clock=_Clock(T_ACTION + timedelta(hours=1)),
    )
    state = await verifier.verify(oid)
    assert (state["verification"], state["status"], state["support_n"]) == (
        "resolved",
        "believed_true",
        1,
    )
    [view] = await repo.assertions([aid])
    assert view.network_confirmed_on == [NODE]
    assert NODE not in {b.entity_id for b in view.bindings}
