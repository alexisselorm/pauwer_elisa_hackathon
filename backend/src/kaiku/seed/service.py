"""Seeding as one async operation, used by the CLI and the tests.

With the demo profile, the gold layer is written minus the forwarding-stall knowledge
(`profiles.demo_exclusions`): the live capture of Elisa's chat must be the first time that
knowledge enters the system (plan 001 §3.5). The writer loads the whole gold layer; the
exclusion is applied here, right after, in the same run.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import LiteralString

from neo4j import AsyncDriver
from pydantic import BaseModel

from kaiku.config import Settings
from kaiku.graph import schema
from kaiku.graph.checks import CheckResult, run_checks
from kaiku.graph.driver import create_driver
from kaiku.logging import get_logger
from kaiku.seed import writer
from kaiku.seed.profiles import Profile, demo_exclusions
from kaiku.seed.sources import MockData

log = get_logger(__name__)

_DELETE_GOLD: LiteralString = (
    "MATCH (a:ContextAssertion {origin: 'gold'}) WHERE a.assertion_id IN $ids "
    "DETACH DELETE a RETURN count(*) AS n"
)
# A kept gold assertion whose successor is excluded was never superseded in the demo's
# world: it is believed again, with no end (R4b no longer applies).
_REOPEN_GOLD: LiteralString = (
    "MATCH (a:ContextAssertion {origin: 'gold'}) WHERE a.assertion_id IN $ids "
    "SET a.status = 'believed_true', a.valid_to = null RETURN count(*) AS n"
)


class SeedRequest(BaseModel):
    network: bool = False
    gold: bool = False
    wipe_gold: bool = False
    profile: Profile = Profile.DEMO

    def validate_rules(self) -> None:
        if not (self.network or self.gold or self.wipe_gold):
            raise ValueError("nothing to do: pass --network, --gold and/or --wipe-gold")
        if self.gold and self.wipe_gold:
            raise ValueError("--gold and --wipe-gold together make no sense")


class SeedResult(BaseModel):
    profile: Profile
    migrations_applied: list[str]
    gold_nodes_wiped: int | None = None
    network: writer.SeedReport | None = None
    gold: writer.SeedReport | None = None
    # Demo profile: the gold assertions removed again after the write (plan 001 §3.5).
    gold_excluded: list[str] = []
    checks: list[CheckResult]

    @property
    def violations(self) -> int:
        return sum(c.violations for c in self.checks)

    @property
    def gold_assertions(self) -> int:
        """Gold assertions in the graph after this run."""
        if self.gold is None:
            return 0
        return self.gold.gold_assertions - len(self.gold_excluded)


def reopened_by_exclusion(data: MockData, excluded: Collection[str]) -> list[str]:
    """Kept gold assertions whose successor is excluded (the stale ITSM KB page that
    Elisa's thread superseded): without the successor they are still believed."""
    return sorted(
        a.assertion_id
        for a in data.gold_assertions()
        if a.superseded_by in excluded and a.assertion_id not in excluded
    )


async def exclude_demo_gold(driver: AsyncDriver, database: str, data: MockData) -> list[str]:
    """Remove the forwarding-stall gold from a freshly written gold layer. Returns the ids
    removed. Idempotent: a second run finds nothing to delete."""
    excluded = sorted(demo_exclusions(data).gold_assertion_ids)
    if not excluded:
        return []
    await driver.execute_query(_DELETE_GOLD, ids=excluded, database_=database)
    if reopened := reopened_by_exclusion(data, excluded):
        await driver.execute_query(_REOPEN_GOLD, ids=reopened, database_=database)
    log.info("demo_gold_excluded", excluded=excluded, reopened=reopened)
    return excluded


async def seed(settings: Settings, request: SeedRequest) -> SeedResult:
    request.validate_rules()
    data = MockData(settings.data_dir)
    migrations = await schema.apply_migrations(settings)
    driver = create_driver(settings)
    try:
        await driver.verify_connectivity()
        db = settings.neo4j_database
        result = SeedResult(profile=request.profile, migrations_applied=migrations, checks=[])
        if request.wipe_gold:
            result.gold_nodes_wiped = await writer.wipe_gold(driver, db)
        if request.network:
            result.network = await writer.write_network(driver, db, data)
        if request.gold:
            result.gold = await writer.write_gold(driver, db, data)
            if request.profile is Profile.DEMO:
                result.gold_excluded = await exclude_demo_gold(driver, db, data)
        result.checks = await run_checks(driver, db)
    finally:
        await driver.close()
    log.info("seed_complete", profile=request.profile, violations=result.violations)
    return result
