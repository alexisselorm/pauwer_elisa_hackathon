"""The one async Neo4j driver per process, and its readiness state.

The driver connects lazily, so creating it never fails when Neo4j is down. A
background task keeps trying `verify_connectivity()` and then runs the migrations,
with exponential backoff. Until that succeeds, `/readyz` returns 503 and the process
stays up: no crash-loop while `docker compose` brings Neo4j up.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from neo4j import AsyncDriver, AsyncGraphDatabase

from silentctx.config import Settings
from silentctx.graph import migrations
from silentctx.logging import get_logger

log = get_logger(__name__)


def create_driver(settings: Settings) -> AsyncDriver:
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        connection_timeout=settings.neo4j_connect_timeout_s,
        connection_acquisition_timeout=settings.neo4j_connect_timeout_s,
        max_transaction_retry_time=settings.neo4j_connect_timeout_s * 2,
    )


@dataclass
class GraphState:
    driver: AsyncDriver
    database: str
    migrated: bool = False
    last_error: str | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    async def ping(self, timeout_s: float) -> None:
        """Raise if Neo4j can't answer a trivial query within `timeout_s`."""
        await asyncio.wait_for(
            self.driver.execute_query("RETURN 1 AS ok", database_=self.database),
            timeout=timeout_s,
        )

    async def bootstrap(self, initial_backoff_s: float, max_backoff_s: float) -> None:
        """Connect and migrate, retrying until it works or the task is cancelled."""
        backoff = initial_backoff_s
        attempt = 0
        while True:
            attempt += 1
            try:
                await self.driver.verify_connectivity()
                applied = await migrations.migrate(self.driver, self.database)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001  # any failure is retried, never fatal
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "neo4j_not_ready",
                    attempt=attempt,
                    retry_in_s=round(backoff, 1),
                    error=self.last_error,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff_s)
                continue
            self.migrated = True
            self.last_error = None
            self.ready.set()
            log.info("neo4j_ready", attempt=attempt, migrations_applied=applied)
            return
