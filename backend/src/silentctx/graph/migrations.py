"""Forward-only Cypher migrations.

Files live in `graph/cypher_migrations/NNNN_name.cypher`. Statements are separated by `;`
at the end of a line, and each one runs in its own auto-commit transaction, because
Neo4j doesn't allow schema and data changes in the same transaction. A file is
recorded as `(:SchemaMigration {id})` once all its statements have run, so statements
must be idempotent (`IF NOT EXISTS`) in case a file is interrupted half way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from neo4j import AsyncDriver

from silentctx.logging import get_logger

MIGRATIONS_DIR = Path(__file__).parent / "cypher_migrations"
_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.cypher$")
_STATEMENT_END = re.compile(r";\s*$", re.MULTILINE)
log = get_logger(__name__)


@dataclass(frozen=True)
class Migration:
    id: str
    path: Path

    def statements(self) -> list[str]:
        lines = [
            line
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("//")
        ]
        parts = _STATEMENT_END.split("\n".join(lines))
        return [p.strip() for p in parts if p.strip()]


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.cypher")):
        m = _FILENAME.match(path.name)
        if not m:
            raise ValueError(f"bad migration filename {path.name!r}; expected NNNN_name.cypher")
        if m.group(1) in seen:
            raise ValueError(f"duplicate migration number {m.group(1)}")
        seen.add(m.group(1))
        found.append(Migration(id=path.stem, path=path))
    return found


async def applied_ids(driver: AsyncDriver, database: str) -> set[str]:
    records, _, _ = await driver.execute_query(
        "MATCH (m:SchemaMigration) RETURN m.id AS id", database_=database
    )
    return {r["id"] for r in records}


async def pending(
    driver: AsyncDriver, database: str, directory: Path = MIGRATIONS_DIR
) -> list[Migration]:
    done = await applied_ids(driver, database)
    return [m for m in discover(directory) if m.id not in done]


async def migrate(
    driver: AsyncDriver, database: str, directory: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply pending migrations in order. Returns the ids applied by this call."""
    applied: list[str] = []
    for migration in await pending(driver, database, directory):
        async with driver.session(database=database) as session:
            for statement in migration.statements():
                result = await session.run(statement)
                await result.consume()
            result = await session.run(
                "MERGE (m:SchemaMigration {id: $id}) "
                "ON CREATE SET m.applied_at = datetime.realtime('UTC')",
                id=migration.id,
            )
            await result.consume()
        log.info("migration_applied", migration=migration.id)
        applied.append(migration.id)
    return applied
