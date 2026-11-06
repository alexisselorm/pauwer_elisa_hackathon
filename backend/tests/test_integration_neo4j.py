"""Against a real Neo4j (testcontainers). Run: uv run pytest -m integration"""

from __future__ import annotations

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
