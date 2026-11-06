"""Against a real Neo4j (testcontainers). Run: uv run pytest -m integration"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from silentctx.config import Settings
from silentctx.main import create_app

NEO4J_IMAGE = "neo4j:2026.10.1-community"  # same image as docker-compose (INF-0.3)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def neo4j_url() -> Iterator[tuple[str, str]]:
    from testcontainers.community.neo4j import Neo4jContainer

    try:
        container = Neo4jContainer(NEO4J_IMAGE, password="integration-pw")
        container.start()
    except Exception as exc:  # noqa: BLE001  # no Docker on this machine
        pytest.skip(f"Docker/Neo4j unavailable: {exc}")
    try:
        yield container.get_connection_url(), "integration-pw"
    finally:
        container.stop()


def _wait_ready(client: TestClient, timeout_s: float = 30.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while True:
        r = client.get("/readyz")
        if r.status_code == 200 or time.monotonic() > deadline:
            return {"code": r.status_code, **r.json()}
        time.sleep(0.2)


def test_readyz_ok_and_migrations_applied_once(
    neo4j_url: tuple[str, str], make_settings: Callable[..., Settings]
) -> None:
    url, password = neo4j_url
    settings = make_settings(neo4j_uri=url, neo4j_password=password)

    with TestClient(create_app(settings)) as client:
        ready = _wait_ready(client)
        assert ready["code"] == 200, ready
        checks = ready["checks"]
        assert isinstance(checks, dict)
        assert checks["neo4j"] == {"status": "ok"}
        assert checks["migrations"] == {"status": "ok"}
        assert ready["status"] == "degraded"  # no LLM keys in tests

    # A restart doesn't re-apply anything.
    with TestClient(create_app(settings)) as client:
        assert _wait_ready(client)["code"] == 200

    from neo4j import GraphDatabase

    with GraphDatabase.driver(url, auth=("neo4j", password)) as driver:
        records, _, _ = driver.execute_query(
            "MATCH (m:SchemaMigration) RETURN m.id AS id ORDER BY id"
        )
        assert [r["id"] for r in records] == ["0000_schema_migrations"]
        records, _, _ = driver.execute_query(
            "SHOW CONSTRAINTS YIELD name WHERE name = 'schema_migration_id' RETURN name"
        )
        assert len(records) == 1
