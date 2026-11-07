"""Schema migrations (via `neo4j-python-migrations`) and the vector-index dimension check.

Migrations live in `graph/migrations/V<version>__<name>.cypher` (the Neo4j-Migrations
naming convention). Applied versions are recorded as `(:__Neo4jMigration)` nodes with a
checksum, so re-running is a no-op and editing an applied file is refused.
The tool is built on the sync driver, so it runs in a worker thread with its own
short-lived driver.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from neo4j import AsyncDriver, GraphDatabase
from neo4j_python_migrations.executor import Executor
from neo4j_python_migrations.migration import Migration
from pydantic import BaseModel

from kaiku.config import Settings
from kaiku.logging import get_logger

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
VECTOR_INDEXES: tuple[str, ...] = ("assertion_embedding", "chunk_embedding")
log = get_logger(__name__)


class EmbeddingDimensionMismatchError(RuntimeError):
    """EMBEDDING_DIM differs from a vector index's dimensions. Writes would fail, so the
    API must not become ready. Fix: set EMBEDDING_DIM, or add a migration that recreates
    the index."""


class VectorIndex(BaseModel):
    name: str
    dimensions: int | None


def _apply_migrations_sync(settings: Settings) -> list[str]:
    applied: list[str] = []

    def on_apply(migration: Migration) -> None:
        applied.append(migration.version)
        log.info("migration_applied", version=migration.version, name=migration.description)

    auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
    with GraphDatabase.driver(
        settings.neo4j_uri, auth=auth, notifications_min_severity="WARNING"
    ) as driver:
        Executor(driver, MIGRATIONS_DIR, database=settings.neo4j_database).migrate(on_apply)
    return applied


async def apply_migrations(settings: Settings) -> list[str]:
    """Apply pending migrations. Returns the versions applied by this call."""
    return await asyncio.to_thread(_apply_migrations_sync, settings)


async def vector_indexes(driver: AsyncDriver, database: str) -> list[VectorIndex]:
    records, _, _ = await driver.execute_query(
        "SHOW VECTOR INDEXES YIELD name, options "
        "RETURN name, options.indexConfig['vector.dimensions'] AS dimensions",
        database_=database,
    )
    return [VectorIndex.model_validate(dict(r)) for r in records]


async def check_embedding_dimensions(driver: AsyncDriver, database: str, expected: int) -> None:
    found = {i.name: i for i in await vector_indexes(driver, database)}
    for name in VECTOR_INDEXES:
        index = found.get(name)
        if index is None:
            raise EmbeddingDimensionMismatchError(f"vector index {name!r} is missing")
        if index.dimensions != expected:
            raise EmbeddingDimensionMismatchError(
                f"vector index {name!r} has {index.dimensions} dimensions "
                f"but EMBEDDING_DIM={expected}"
            )
