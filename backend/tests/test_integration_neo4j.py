"""Against a real Neo4j (testcontainers). Run: uv run pytest -m integration"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from neo4j import GraphDatabase

from silentctx.config import Settings
from silentctx.graph import schema
from silentctx.graph.cypher import vector
from silentctx.main import create_app

NEO4J_IMAGE = "neo4j:2026.10.1-community"  # same image as docker-compose.yml
PASSWORD = "integration-pw"

pytestmark = pytest.mark.integration

CONSTRAINTS = {
    "entity_id_unique",
    "assertion_id_unique",
    "event_id_unique",
    "thread_id_unique",
    "person_id_unique",
    "gap_id_unique",
    "chunk_id_unique",
}
RANGE_INDEXES = {
    "assertion_valid_from",
    "assertion_valid_to",
    "assertion_recorded_at",
    "assertion_status",
    "assertion_topic",
    "entity_valid_from",
    "entity_valid_to",
    "event_time",
}


@pytest.fixture(scope="module")
def neo4j_url() -> Iterator[str]:
    from testcontainers.community.neo4j import Neo4jContainer

    try:
        container = Neo4jContainer(NEO4J_IMAGE, password=PASSWORD)
        container.start()
    except Exception as exc:  # noqa: BLE001  # no Docker on this machine
        pytest.skip(f"Docker/Neo4j unavailable: {exc}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
def settings(neo4j_url: str, make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD)


def _query(url: str, cypher: str) -> list[dict[str, Any]]:
    with GraphDatabase.driver(url, auth=("neo4j", PASSWORD)) as driver:
        records, _, _ = driver.execute_query(cypher)
        return [dict(r) for r in records]


def _readyz(client: TestClient, *, until_code: int, timeout_s: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        r = client.get("/readyz")
        if r.status_code == until_code or time.monotonic() > deadline:
            return {"code": r.status_code, **r.json()}
        time.sleep(0.2)


def test_schema_is_applied_and_api_is_ready(neo4j_url: str, settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        ready = _readyz(client, until_code=200)
    assert ready["code"] == 200, ready
    assert ready["checks"]["neo4j"] == {"status": "ok"}
    assert ready["checks"]["schema"] == {"status": "ok"}
    assert ready["status"] == "degraded"  # no LLM keys in tests

    names = {r["name"] for r in _query(neo4j_url, "SHOW CONSTRAINTS YIELD name RETURN name")}
    assert names >= CONSTRAINTS
    indexes = {
        r["name"]: r
        for r in _query(
            neo4j_url,
            "SHOW INDEXES YIELD name, type, labelsOrTypes, properties, options "
            "RETURN name, type, labelsOrTypes, properties, options",
        )
    }
    for name in RANGE_INDEXES:
        assert indexes[name]["type"] == "RANGE", name
    for name, label, properties in [
        # V0002: `status` is declared on the index so SEARCH can pre-filter on it.
        ("assertion_embedding", "ContextAssertion", ["embedding", "status"]),
        ("chunk_embedding", "SourceChunk", ["embedding"]),
    ]:
        index = indexes[name]
        assert index["type"] == "VECTOR"
        assert index["labelsOrTypes"] == [label]
        assert index["properties"] == properties
        config = index["options"]["indexConfig"]
        assert config["vector.dimensions"] == 1024
        assert config["vector.similarity_function"].lower() == "cosine"


async def test_rerunning_migrations_is_a_no_op(neo4j_url: str, settings: Settings) -> None:
    await schema.apply_migrations(settings)  # whatever is still pending
    assert await schema.apply_migrations(settings) == []
    versions = _query(
        neo4j_url,
        "MATCH (m:__Neo4jMigration) WHERE m.version <> 'BASELINE' RETURN m.version AS v",
    )
    expected = sorted(p.name[1:5] for p in schema.MIGRATIONS_DIR.glob("V*.cypher"))
    assert sorted(r["v"] for r in versions) == expected


def test_restart_applies_nothing_and_is_ready(settings: Settings) -> None:
    for _ in range(2):
        with TestClient(create_app(settings)) as client:
            assert _readyz(client, until_code=200)["code"] == 200


def test_embedding_dim_mismatch_refuses_readiness(
    neo4j_url: str, make_settings: Callable[..., Settings]
) -> None:
    wrong = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD, embedding_dim=4096)
    app = create_app(wrong)
    with TestClient(app) as client:
        deadline = time.monotonic() + 60
        while app.state.ctx.graph.fatal_error is None and time.monotonic() < deadline:
            time.sleep(0.2)
        ready = _readyz(client, until_code=503, timeout_s=1)
    assert ready["code"] == 503
    assert ready["checks"]["neo4j"] == {"status": "ok"}
    assert ready["checks"]["schema"]["status"] == "down"
    assert "EMBEDDING_DIM=4096" in ready["checks"]["schema"]["error"]


# --- Vector search (BE-1.2) ------------------------------------------------------------------


def _unit(i: int, dim: int = 1024, *, mix: tuple[int, float] | None = None) -> list[float]:
    v = [0.0] * dim
    v[i] = 1.0
    if mix:
        v[mix[0]] = mix[1]
    return v


async def test_vector_search_and_scoped_ranking(neo4j_url: str, settings: Settings) -> None:
    from neo4j import AsyncGraphDatabase

    await schema.apply_migrations(settings)
    rows = [
        {"id": "VEC-a", "status": "believed_true", "e": _unit(0)},
        {"id": "VEC-b", "status": "disputed", "e": _unit(0, mix=(1, 0.1))},
        {"id": "VEC-c", "status": "believed_true", "e": _unit(1)},
        {"id": "VEC-d", "status": "believed_true", "e": None},
    ]
    q = _unit(0)
    async with AsyncGraphDatabase.driver(neo4j_url, auth=("neo4j", PASSWORD)) as driver:
        await driver.execute_query(
            "UNWIND $rows AS r CREATE (:ContextAssertion "
            "{assertion_id: r.id, status: r.status, embedding: r.e})",
            rows=rows,
        )
        await driver.execute_query("CALL db.awaitIndexes(60)")
        try:
            hits = await vector.search(driver, "neo4j", vector.ASSERTIONS, q, 2)
            assert [h.id for h in hits] == ["VEC-a", "VEC-b"]

            # Pre-filter inside SEARCH: the best disputed match, even though it isn't top-1.
            hits = await vector.search(
                driver, "neo4j", vector.ASSERTIONS, q, 1, status_in=["disputed"]
            )
            assert [h.id for h in hits] == ["VEC-b"]

            # The deprecated procedure path gives the same answer.
            hits = await vector._search_procedure(
                driver, "neo4j", vector.ASSERTIONS, q, 1, ["disputed"]
            )
            assert [h.id for h in hits] == ["VEC-b"]

            # Scoped ranking: the trap case. SEARCH with a MATCH filter would return nothing
            # here (post-filter of the global top-1); exact ranking returns VEC-c.
            hits = await vector.rank_candidates(
                driver, "neo4j", vector.ASSERTIONS, ["VEC-c", "VEC-d"], q, 1
            )
            assert [h.id for h in hits] == ["VEC-c"]
            assert await vector.rank_candidates(driver, "neo4j", vector.ASSERTIONS, [], q, 1) == []
        finally:
            await driver.execute_query(
                "MATCH (n:ContextAssertion) WHERE n.assertion_id STARTS WITH 'VEC-' DETACH DELETE n"
            )


async def test_vector_search_rejects_bad_arguments(settings: Settings) -> None:
    from neo4j import AsyncGraphDatabase

    async with AsyncGraphDatabase.driver(settings.neo4j_uri, auth=("neo4j", PASSWORD)) as driver:
        with pytest.raises(ValueError, match="k must be"):
            await vector.search(driver, "neo4j", vector.ASSERTIONS, _unit(0), 0)
        with pytest.raises(ValueError, match="can't filter"):
            await vector.search(driver, "neo4j", vector.CHUNKS, _unit(0), 1, status_in=["x"])


# --- Seed loader (BE-1.4) ----------------------------------------------------------------------

MOCK_DATA = Path(__file__).parents[2] / "mock_data"


async def _snapshot(url: str) -> dict[str, Any]:
    """Every seed/gold node with its labels, properties and outgoing relationships."""
    rows = _query(
        url,
        "MATCH (n) WHERE n.origin IN ['seed', 'gold'] "
        "OPTIONAL MATCH (n)-[r]->(m) "
        "WITH n, r, m ORDER BY type(r), coalesce(m.entity_id, m.assertion_id, m.record_id) "
        "RETURN coalesce(n.entity_id, n.assertion_id, n.event_id, n.snapshot_id, n.record_id) "
        "AS id, "
        "labels(n) AS labels, properties(n) AS props, "
        "collect([type(r), properties(r), "
        "coalesce(m.entity_id, m.assertion_id, m.record_id)]) AS rels",
    )
    return {r["id"]: (sorted(r["labels"]), r["props"], r["rels"]) for r in rows}


async def test_seed_full_is_clean_idempotent_and_wipeable(
    neo4j_url: str, make_settings: Callable[..., Settings]
) -> None:
    from silentctx.seed.profiles import Profile
    from silentctx.seed.service import SeedRequest, seed

    settings = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA)
    full = SeedRequest(network=True, gold=True, profile=Profile.FULL)

    first = await seed(settings, full)
    assert first.violations == 0, [c for c in first.checks if c.violations]
    assert first.network is not None
    assert first.gold is not None
    assert first.network.entities == 169  # 166 entities + Gen1..Gen3
    assert first.gold.gold_assertions == 24
    before = await _snapshot(neo4j_url)

    second = await seed(settings, full)
    assert second.violations == 0
    assert second.network is not None
    assert second.gold is not None
    assert second.network.pruned == {}
    assert second.gold.pruned == {}
    assert await _snapshot(neo4j_url) == before  # a re-run changes nothing

    # Elisa's binding: node, class and activity.
    bindings = _query(
        neo4j_url,
        "MATCH (:ContextAssertion {assertion_id: 'ASR-0001'})-[r:ABOUT]->(e) "
        "RETURN r.scope AS scope, e.entity_id AS id ORDER BY scope",
    )
    assert [(b["scope"], b["id"]) for b in bindings] == [
        ("activity", "TOPIC:forwarding_state_after_sfp_swap"),
        ("applies_to", "TNC-1X"),
        ("observed_on", "TRN-042"),
    ]

    # As of a Kemi dispatch on 2026-08-10, only the code lock is in scope (R9).
    kemi = _query(
        neo4j_url,
        "WITH datetime('2026-08-10T06:00:00Z') AS t "
        "MATCH (a:ContextAssertion)-[:ABOUT {scope: 'observed_on'}]->"
        "(:Segment {entity_id: 'SEG-042'}) "
        "WHERE a.valid_from <= t AND t < coalesce(a.valid_to, datetime('9999-12-31T00:00:00Z')) "
        "RETURN a.assertion_id AS id",
    )
    assert [k["id"] for k in kemi] == ["ASR-0024"]

    wiped = await seed(settings, SeedRequest(wipe_gold=True, profile=Profile.FULL))
    assert wiped.gold_nodes_wiped == 29  # 24 assertions + 5 access records
    assert _query(neo4j_url, "MATCH (n {origin: 'gold'}) RETURN count(n) AS n")[0]["n"] == 0
    assert wiped.violations == 0


async def test_seed_prunes_rows_that_left_the_source(
    neo4j_url: str, make_settings: Callable[..., Settings]
) -> None:
    from silentctx.seed.service import SeedRequest, seed

    settings = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA)
    _query(
        neo4j_url,
        "MATCH (d:Device {entity_id: 'TRN-004'}) "
        "CREATE (:Event:Alarm {event_id: 'ALM-STALE', origin: 'seed', "
        "event_time: datetime('2030-01-01T00:00:00Z')})-[:ON]->(d)",
    )
    _query(
        neo4j_url, "CREATE (:ContextAssertion {assertion_id: 'X-EXTRACTED', origin: 'extracted'})"
    )
    result = await seed(settings, SeedRequest(network=True))
    assert result.network is not None
    assert result.network.pruned.get("events") == 1
    assert result.violations == 0
    # Extracted knowledge is never pruned by the seed.
    assert (
        _query(neo4j_url, "MATCH (n {assertion_id: 'X-EXTRACTED'}) RETURN count(n) AS n")[0]["n"]
        == 1
    )
    _query(neo4j_url, "MATCH (n {assertion_id: 'X-EXTRACTED'}) DELETE n")


# --- MCP tools over HTTP (MCP-1, MCP-2) ------------------------------------------------------

ALL_TOOLS = {
    "ping",
    "resolve_entities",
    "get_entity",
    "get_context",
    "search_context",
    "check_conditions",
    "get_site_access",
    "get_events",
    "get_history",
    "report_outcome",
    "open_knowledge_gap",
    "propose_assertion",
    "flag_assertion",
}


class _Mcp:
    def __init__(self, client: TestClient, token: str) -> None:
        self.client, self.token, self.n = client, token, 0

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        import json

        self.n += 1
        r = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.token}",
            },
        )
        assert r.status_code == 200, r.text
        for line in r.text.splitlines():
            if line.startswith("data: "):
                return dict(json.loads(line[6:]))
        return dict(r.json())

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        result = self.rpc("tools/call", {"name": tool, "arguments": args})["result"]
        return dict(result)


async def test_mcp_tools_end_to_end(neo4j_url: str, make_settings: Callable[..., Settings]) -> None:
    from silentctx.seed.profiles import Profile
    from silentctx.seed.service import SeedRequest, seed
    from tests.conftest import AGENT_TOKEN

    settings = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA)
    seeded = await seed(settings, SeedRequest(network=True, gold=True, profile=Profile.FULL))
    assert seeded.violations == 0

    with TestClient(create_app(settings)) as client:
        assert _readyz(client, until_code=200)["code"] == 200
        mcp = _Mcp(client, AGENT_TOKEN)

        tools = {t["name"] for t in mcp.rpc("tools/list", {})["result"]["tools"]}
        assert tools == ALL_TOOLS  # and nothing that confirms, retracts or edits validity

        r = mcp.call("resolve_entities", text="I swapped the SFP on the Pori node")
        assert r["structuredContent"]["candidates"][0]["entity_id"] == "TRN-017"

        r = mcp.call("get_entity", entity_id="TRN-017")["structuredContent"]
        assert (r["model"], r["generation"], r["exists_at"]) == ("TNC-1X", "Gen1", True)

        ctx = mcp.call("get_context", entity_id="TRN-017")["structuredContent"]
        top = ctx["assertions"][0]
        assert top["assertion_id"] == "ASR-0001"
        assert top["match"] == "model"  # reached through TNC-1X
        assert top["qualifiers"]["avoid"] == ["replace_sfp", "reboot_node"]

        cond = mcp.call(
            "check_conditions",
            assertion_id="ASR-0001",
            entity_id="TRN-042",
            at="2025-10-14T06:21:30Z",
        )["structuredContent"]
        assert cond["all_met"] is True
        assert cond["port"] == "1/1/3"

        # Kemi on 2026-08-10: the code lock today; the padlock as it was known then (R10).
        now = mcp.call("get_site_access", entity_or_site="TRN-042", as_of="2026-08-10T06:00:00Z")
        assert [a["assertion_id"] for a in now["structuredContent"]["assertions"]] == ["ASR-0024"]
        then = mcp.call(
            "get_site_access",
            entity_or_site="TRN-042",
            as_of="2026-08-10T06:00:00Z",
            known_at="2026-08-10T06:00:00Z",
        )["structuredContent"]
        assert "ASR-0023" in [a["assertion_id"] for a in then["assertions"]]
        assert then["access_register"][0]["lock"] == "padlock"

        events = mcp.call(
            "get_events",
            entity_id="TRN-042",
            start="2025-10-14T00:00:00Z",
            end="2025-10-15T00:00:00Z",
        )["structuredContent"]
        assert any(e["type"] == "MODULE_INSERTED" for e in events["events"])

        history = mcp.call("get_history", assertion_id="ASR-0023")["structuredContent"]
        assert "ASR-0024" in [v["assertion_id"] for v in history["versions"]]

        search = mcp.call("search_context", query="zero traffic after replacing the SFP")
        assert search["structuredContent"]["assertions"]

        # Writes: idempotent, attributed, governed.
        args = {
            "entity_id": "TRN-042",
            "action": "bounce_interface",
            "result": "resolved",
            "assertion_id": "ASR-0001",
            "observed_at": "2025-10-14T06:21:30Z",
            "idempotency_key": "it-outcome-1",
        }
        first = mcp.call("report_outcome", **args)["structuredContent"]
        again = mcp.call("report_outcome", **args)["structuredContent"]
        assert first["evidence"] == "SUPPORTS"
        assert first["conditions_met"] is True
        assert again["replayed"] is True
        assert again["event_id"] == first["event_id"]
        assert again["support_n"] == first["support_n"]

        gap = mcp.call(
            "open_knowledge_gap",
            question="Which firmware fixes the forwarding bug?",
            entity_ids=["TNC-1X"],
            idempotency_key="it-gap-1",
            on_behalf_of="U-ELISA",
            trigger_event_id=first["event_id"],
        )["structuredContent"]
        assert gap["routed_to"] == "erkki"
        [act] = _query(
            neo4j_url,
            "MATCH (e:AgentAction {tool: 'open_knowledge_gap'}) "
            "OPTIONAL MATCH (e)-[:TRIGGERED_BY]->(t:Event) "
            "RETURN e.on_behalf_of AS by, e.trigger_event_id AS trigger, t.event_id AS linked",
        )
        assert act == {"by": "U-ELISA", "trigger": first["event_id"], "linked": first["event_id"]}

        # ST-52: an unknown entity is a tool error and nothing is written.
        writes = "MATCH (n) WHERE n:Outcome OR n:KnowledgeGap OR n:AgentAction RETURN count(n) AS n"
        before = _query(neo4j_url, writes)
        for tool, call_args in [
            ("report_outcome", {**args, "entity_id": "NOPE-1", "idempotency_key": "it-nope-1"}),
            (
                "open_knowledge_gap",
                {"question": "q?", "entity_ids": ["NOPE-1"], "idempotency_key": "it-nope-2"},
            ),
        ]:
            refused_write = mcp.call(tool, **call_args)
            assert refused_write["isError"] is True
            assert "NOPE-1" in refused_write["content"][0]["text"]
        assert _query(neo4j_url, writes) == before

        proposal = mcp.call(
            "propose_assertion",
            statement="A bounce also clears the stall on TNC-2X",
            entity_ids=["TRN-010", "TNC-2X"],
            evidence_event_ids=[first["event_id"]],
            rationale="bounce worked",
            idempotency_key="it-prop-1",
        )["structuredContent"]
        assert proposal["status"] == "unverified"

        flag = mcp.call(
            "flag_assertion", assertion_id="ASR-0030", reason="stale", idempotency_key="it-f-1"
        )["structuredContent"]
        assert flag["review_due"] is True

        refused = mcp.call(
            "propose_assertion",
            statement="no evidence",
            entity_ids=["TRN-010"],
            evidence_event_ids=["EV-NOPE"],
            rationale="r",
            idempotency_key="it-prop-2",
        )
        assert refused["isError"] is True
        assert "unknown events" in refused["content"][0]["text"]
        unknown = mcp.call("get_entity", entity_id="NOPE-1")
        assert unknown["isError"] is True

        action = _query(
            neo4j_url,
            "MATCH (e:AgentAction {tool: 'report_outcome'}) RETURN e.agent_id AS agent",
        )
        assert [a["agent"] for a in action] == ["ops-agent"]  # from the token, once

        prompt = mcp.rpc("prompts/get", {"name": "ops_triage"})["result"]
        assert "get_context" in prompt["messages"][0]["content"]["text"]
        resource = mcp.rpc("resources/read", {"uri": "context://entity/TRN-017"})["result"]
        assert "ASR-0001" in resource["contents"][0]["text"]


async def test_temporal_reads_r9_r10(
    neo4j_url: str, make_settings: Callable[..., Settings]
) -> None:
    """BE-1.6: Kemi before the lock change, the as-running model gating a model rule, and
    the site level of the scope."""
    from silentctx.seed.profiles import Profile
    from silentctx.seed.service import SeedRequest, seed
    from tests.conftest import AGENT_TOKEN

    settings = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA)
    seeded = await seed(settings, SeedRequest(network=True, gold=True, profile=Profile.FULL))
    assert seeded.violations == 0
    # A TNC-1X by the books that runs TNC-2X hardware now, and stands at the Kemi site.
    _query(
        neo4j_url,
        "MATCH (m:HwModel {entity_id: 'TNC-1X'}) MATCH (site:Site {entity_id: 'FI-KEM-DC4'}) "
        "CREATE (d:Entity:Device {entity_id: 'IT-SWAP', entity_type: 'device', "
        "  valid_from: datetime('2020-01-01T00:00:00Z')}) "
        "CREATE (d)-[:IS_MODEL {valid_from: datetime('2020-01-01T00:00:00Z')}]->(m) "
        "CREATE (d)-[:AT_SITE {valid_from: datetime('2020-01-01T00:00:00Z')}]->(site) "
        "CREATE (:RunningSnapshot {snapshot_id: 'IT-SWAP-1', hw_model: 'TNC-2X', "
        "  valid_from: datetime('2026-01-01T00:00:00Z')})-[:SNAPSHOT_OF]->(d)",
    )
    try:
        with TestClient(create_app(settings)) as client:
            assert _readyz(client, until_code=200)["code"] == 200
            mcp = _Mcp(client, AGENT_TOKEN)

            # Kemi before the change (2026-08-02): Pekka's padlock.
            july = mcp.call(
                "get_site_access", entity_or_site="TRN-042", as_of="2026-07-01T06:00:00Z"
            )["structuredContent"]
            # ASR-0032 is the older ITSM KB line, also "ask Pekka" until the change.
            assert {a["assertion_id"] for a in july["assertions"]} == {"ASR-0023", "ASR-0032"}
            assert july["access_register"][0]["lock"] == "padlock"
            assert july["access_register"][0]["holder"].startswith("Pekka")

            # R9: the TNC-1X rule reaches it by model while it ran TNC-1X, not after.
            def ids(as_of: str) -> list[str]:
                ctx = mcp.call("get_context", entity_id="IT-SWAP", as_of=as_of)
                return [a["assertion_id"] for a in ctx["structuredContent"]["assertions"]]

            assert "ASR-0001" in ids("2025-12-01T00:00:00Z")  # no snapshot: as-built model
            assert "ASR-0001" not in ids("2026-03-01T00:00:00Z")

            ctx = mcp.call("get_context", entity_id="IT-SWAP")["structuredContent"]
            assert {"IT-SWAP", "TNC-1X", "Gen1", "FI-KEM-DC4"} <= set(ctx["scope_entities"])
    finally:
        _query(
            neo4j_url,
            "MATCH (n) WHERE n.entity_id = 'IT-SWAP' OR n.snapshot_id = 'IT-SWAP-1' "
            "DETACH DELETE n",
        )


async def test_sim_events_reach_the_event_stream(neo4j_url: str, settings: Settings) -> None:
    """SIM-1: fault events land as (:Event:Alarm)-[:ON]->(:Device), so get_events sees them;
    publishing twice writes nothing new."""
    from silentctx.graph.driver import create_driver
    from silentctx.sim.app import publish
    from silentctx.sim.network import Simulator

    _query(neo4j_url, "MERGE (:Entity:Device {entity_id: 'TRN-017'})")
    sim = Simulator()
    events = sim.fault_sfp_swap_stall("TRN-017", "1/1/2") + sim.bounce("TRN-017", "1/1/2")
    driver = create_driver(settings)
    try:
        await publish(driver, settings.neo4j_database, events)
        await publish(driver, settings.neo4j_database, events)
    finally:
        await driver.close()
    rows = _query(
        neo4j_url,
        "MATCH (e:Event {origin: 'sim'})-[:ON]->(:Device {entity_id: 'TRN-017'}) "
        "RETURN e.type AS type, labels(e) AS labels, e.port AS port ORDER BY e.event_time",
    )
    assert [r["type"] for r in rows] == [
        "MODULE_INSERTED",
        "LINK_UP",
        "OPTICAL_POWER_OK",
        "IF_DISABLED",
        "IF_ENABLED",
    ]
    assert "Alarm" in rows[0]["labels"]
    assert "DeviceEvent" in rows[-1]["labels"]
    assert {r["port"] for r in rows} == {"1/1/2"}


class _SimClock:
    """Real time, plus whatever the test fast-forwards."""

    def __init__(self) -> None:
        from datetime import UTC, datetime, timedelta

        self.offset = timedelta(0)
        self._now = lambda: datetime.now(UTC)

    def __call__(self) -> Any:
        return self._now() + self.offset


@contextlib.contextmanager
def _serve(app: Any) -> Iterator[str]:
    """An ASGI app as a real HTTP server on a free port, in a thread."""
    import socket
    import threading

    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@contextlib.contextmanager
def _sim_server(clock: _SimClock, settings: Settings) -> Iterator[str]:
    """The simulator as a real HTTP server (the api pulls from it over the network)."""
    from silentctx.sim.app import create_app as create_sim_app
    from silentctx.sim.network import Simulator

    with _serve(create_sim_app(Simulator(clock=clock), settings=settings)) as url:
        yield url


def test_live_demo_fault_to_network_confirmed_outcome(
    neo4j_url: str, make_settings: Callable[..., Settings]
) -> None:
    """ST-30 over MCP with the simulator at "now": a stall on TRN-017 1/1/2 meets all three
    conditions of the TRN-042 rule (reached through TNC-1X), the fault events are on record,
    a bounce recovers the counters, and the outcome is network-confirmed evidence."""
    import asyncio
    from datetime import timedelta

    import httpx

    from silentctx.seed.profiles import Profile
    from silentctx.seed.service import SeedRequest, seed
    from tests.conftest import AGENT_TOKEN

    base = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA)
    assert (
        asyncio.run(
            seed(base, SeedRequest(network=True, gold=True, profile=Profile.FULL))
        ).violations
        == 0
    )
    clock = _SimClock()
    node = {"node": "TRN-017", "port": "1/1/2"}

    with _sim_server(clock, base) as sim_url:
        settings = make_settings(
            neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA, sim_url=sim_url
        )
        with TestClient(create_app(settings)) as client:
            assert _readyz(client, until_code=200)["code"] == 200
            mcp = _Mcp(client, AGENT_TOKEN)

            def conditions(at: Any) -> dict[str, Any]:
                return mcp.call(
                    "check_conditions",
                    assertion_id="ASR-0001",
                    entity_id="TRN-017",
                    port="1/1/2",
                    at=at.isoformat(),
                )["structuredContent"]

            healthy = conditions(clock())
            assert {c["name"]: c["status"] for c in healthy["conditions"]}["counters_zero"] == (
                "not_met"  # ST-31: normal counters, no bounce
            )

            fault = httpx.post(f"{sim_url}/sim/fault", json={**node, "kind": "sfp_swap_stall"})
            assert fault.status_code == 200
            before = conditions(clock())
            assert before["all_met"] is True, before
            assert [c["status"] for c in before["conditions"]] == ["met"] * 3

            def event_types() -> list[str]:
                got = mcp.call(
                    "get_events",
                    entity_id="TRN-017",
                    start=(clock() - timedelta(hours=3)).isoformat(),
                    end=(clock() + timedelta(minutes=1)).isoformat(),
                )["structuredContent"]["events"]
                return [e["type"] for e in got]

            deadline = time.monotonic() + 15
            while "OPTICAL_POWER_OK" not in event_types() and time.monotonic() < deadline:
                time.sleep(0.5)  # the sim publishes to Neo4j every 2 s
            assert {"MODULE_INSERTED", "LINK_UP", "OPTICAL_POWER_OK"} <= set(event_types())

            bounced_at = clock()
            r = httpx.post(f"{sim_url}/sim/action", json={**node, "action": "bounce_interface"})
            assert [e["type"] for e in r.json()] == ["IF_DISABLED", "IF_ENABLED"]
            outcome = mcp.call(
                "report_outcome",
                entity_id="TRN-017",
                port="1/1/2",
                action="bounce_interface",
                result="resolved",
                assertion_id="ASR-0001",
                observed_at=bounced_at.isoformat(),
                idempotency_key="it-live-outcome",
            )["structuredContent"]
            assert (outcome["evidence"], outcome["conditions_met"]) == ("SUPPORTS", True)

            clock.offset = timedelta(minutes=31)  # a full interval of recovered traffic
            after = conditions(clock())
            assert after["all_met"] is False
            assert {c["name"]: c["status"] for c in after["conditions"]}["counters_zero"] == (
                "not_met"
            )
            history = mcp.call("get_history", assertion_id="ASR-0001")["structuredContent"]
            assert outcome["event_id"] in [e["event_id"] for e in history["evidence"]]


def test_slack_run_bounce_button_reports_through_mcp(
    neo4j_url: str, make_settings: Callable[..., Settings]
) -> None:
    """SL-2: [Run bounce (simulated)] on the proactive thread bounces the port in the sim,
    checks the live counters, and reports the outcome through the real MCP server, as the
    ops agent, on behalf of the clicking user."""
    import asyncio
    from datetime import timedelta

    import httpx

    from silentctx.agents.ops_agent import AnswerStatus, OpsAgent, OpsAnswer, Step
    from silentctx.seed.profiles import Profile
    from silentctx.seed.service import SeedRequest, seed
    from silentctx.slack import actions as a

    base = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA)
    report = asyncio.run(seed(base, SeedRequest(network=True, gold=True, profile=Profile.FULL)))
    assert report.violations == 0
    clock = _SimClock()

    class FastSim(a.SimNetwork):
        async def bounce(self, node: str, port: str) -> None:
            await super().bounce(node, port)
            clock.offset += timedelta(seconds=35)  # past the ~30 s recovery

    with _sim_server(clock, base) as sim_url:
        api_settings = make_settings(
            neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA, sim_url=sim_url
        )
        with _serve(create_app(api_settings)) as api_url:
            deadline = time.monotonic() + 60
            while httpx.get(f"{api_url}/readyz").status_code != 200:
                assert time.monotonic() < deadline, "api never became ready"
                time.sleep(0.2)
            agent = OpsAgent(make_settings(mcp_url=f"{api_url}/mcp"))
            sim = FastSim(sim_url)
            ops = a.OpsActions(agent, agent, network=sim, settle=timedelta(0), clock=clock)
            asyncio.run(
                sim._request(
                    "POST",
                    "/sim/fault",
                    json={"node": "TRN-017", "port": "1/1/2", "kind": "sfp_swap_stall"},
                )
            )
            advice = OpsAnswer(
                status=AnswerStatus.ANSWERED,
                summary="bounce",
                steps=[Step(action="bounce 1/1/2", assertion_ids=["ASR-0001"])],
            )
            fault = {"ne_id": "TRN-017", "port": "1/1/2"}
            blocks = a.answer_blocks(advice, fault=fault)
            [button] = [e for e in blocks[-1]["elements"] if e["action_id"] == a.RUN_BOUNCE]
            body = {
                "actions": [{"action_id": a.RUN_BOUNCE, "value": button["value"]}],
                "channel": {"id": "CALARM"},
                "message": {"ts": "1.000001", "blocks": blocks},
                "user": {"id": "U5"},
            }

            class Client:
                def __init__(self) -> None:
                    self.updated: list[str] = []

                async def chat_update(self, **kw: Any) -> None:
                    self.updated.append(kw["text"])

            async def ack() -> None:
                return None

            client = Client()
            asyncio.run(ops.on_outcome(ack, body, client))  # type: ignore[arg-type]

    assert "network-confirmed on TRN-017 1/1/2" in client.updated[-1], client.updated
    rows = _query(
        neo4j_url,
        "MATCH (o:Outcome {on_behalf_of: 'U5'})-[r:SUPPORTS]->(:ContextAssertion "
        "{assertion_id: 'ASR-0001'}) RETURN o.result AS result, o.agent_id AS agent, "
        "r.conditions_met AS met",
    )
    assert rows == [{"result": "resolved", "agent": "ops-agent", "met": True}]
