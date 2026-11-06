"""Against a real Neo4j (testcontainers). Run: uv run pytest -m integration"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
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
    assert sorted(r["v"] for r in versions) == ["0001", "0002"]


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
