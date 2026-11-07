"""The one async Neo4j driver per process, and its readiness state.

The driver connects lazily, so creating it never fails when Neo4j is down. A
background task keeps trying `verify_connectivity()`, then applies the migrations and
checks the vector-index dimensions, with exponential backoff. Until that succeeds,
`/readyz` returns 503 and the process stays up: no crash-loop while `docker compose`
brings Neo4j up. A dimension mismatch is a configuration error: it is not retried,
and the API stays unready until the configuration is fixed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from neo4j import AsyncDriver, AsyncGraphDatabase

from kaiku.config import Settings
from kaiku.graph import schema
from kaiku.logging import get_logger

log = get_logger(__name__)


def create_driver(settings: Settings) -> AsyncDriver:
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        connection_timeout=settings.neo4j_connect_timeout_s,
        connection_acquisition_timeout=settings.neo4j_connect_timeout_s,
        max_transaction_retry_time=settings.neo4j_connect_timeout_s * 2,
        # "index already exists" and similar INFORMATION notices are expected and noisy.
        notifications_min_severity="WARNING",
        # Optional properties (retracted_at, permalink, ...) appear only once something
        # writes them; "unknown property" notices would otherwise flood the logs.
        notifications_disabled_classifications=["UNRECOGNIZED"],
    )


@dataclass
class GraphState:
    driver: AsyncDriver
    settings: Settings
    migrated: bool = False
    last_error: str | None = None
    fatal_error: str | None = None  # configuration error: never retried
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def database(self) -> str:
        return self.settings.neo4j_database

    async def ping(self, timeout_s: float) -> None:
        """Raise if Neo4j can't answer a trivial query within `timeout_s`."""
        await asyncio.wait_for(
            self.driver.execute_query("RETURN 1 AS ok", database_=self.database),
            timeout=timeout_s,
        )

    async def bootstrap(self) -> None:
        """Connect, migrate and check the schema, retrying until it works, a configuration
        error is found, or the task is cancelled."""
        backoff = self.settings.neo4j_retry_initial_s
        attempt = 0
        while True:
            attempt += 1
            try:
                await self.driver.verify_connectivity()
                applied = await schema.apply_migrations(self.settings)
                await schema.check_embedding_dimensions(
                    self.driver, self.database, self.settings.embedding_dim
                )
            except asyncio.CancelledError:
                raise
            except schema.EmbeddingDimensionMismatchError as exc:
                self.fatal_error = str(exc)
                log.critical("schema_mismatch_refusing_readiness", error=self.fatal_error)
                return
            except Exception as exc:  # noqa: BLE001  # any other failure is retried
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "neo4j_not_ready",
                    attempt=attempt,
                    retry_in_s=round(backoff, 1),
                    error=self.last_error,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.settings.neo4j_retry_max_s)
                continue
            self.migrated = True
            self.last_error = None
            self.ready.set()
            log.info("neo4j_ready", attempt=attempt, migrations_applied=applied)
            return
