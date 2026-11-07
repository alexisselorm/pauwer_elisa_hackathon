"""Reset the graph to the demo baseline, for the console's Reset button.

Deletes everything that was captured, proposed or recorded since seeding (Slack and email
threads, messages, extracted assertions, outcomes, gaps, flags, MCP tool calls) and every
change those made to seeded knowledge (status, support, supersession), then seeds the
network and the gold knowledge again. The result is what the demo scripts in
`backend/scripts/` expect: the Kemi code lock is known, Lappeenranta's renovation is not.

Kept: the schema migrations (constraints and indexes are schema, not nodes) and the Slack
channel cursors. Without the cursors, the slackbot's catch-up would re-read every old
thread at its next start and capture them all again.
"""

from __future__ import annotations

from typing import LiteralString

from pydantic import BaseModel

from kaiku.config import Settings
from kaiku.graph.driver import create_driver
from kaiku.logging import get_logger
from kaiku.seed.profiles import Profile
from kaiku.seed.service import SeedRequest, SeedResult, seed

log = get_logger(__name__)

_KEEP: LiteralString = "n:__Neo4jMigration OR n:ChannelCursor"
_DELETE_BATCH: LiteralString = (
    "MATCH (n) WHERE NOT (" + _KEEP + ") WITH n LIMIT $batch DETACH DELETE n RETURN count(*) AS n"
)
BATCH = 5000


class ResetResult(BaseModel):
    deleted_nodes: int
    seed: SeedResult


async def reset(settings: Settings) -> ResetResult:
    driver = create_driver(settings)
    deleted = 0
    try:
        while True:  # batches keep each transaction small
            records, _, _ = await driver.execute_query(
                _DELETE_BATCH, batch=BATCH, database_=settings.neo4j_database
            )
            n = int(records[0]["n"])
            deleted += n
            if n < BATCH:
                break
    finally:
        await driver.close()
    log.info("graph_reset_wiped", deleted_nodes=deleted)
    seeded = await seed(settings, SeedRequest(network=True, gold=True, profile=Profile.FULL))
    log.info("graph_reset_complete", violations=seeded.violations)
    return ResetResult(deleted_nodes=deleted, seed=seeded)
