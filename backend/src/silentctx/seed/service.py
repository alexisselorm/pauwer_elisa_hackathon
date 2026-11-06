"""Seeding as one async operation, used by the CLI and the tests."""

from __future__ import annotations

from pydantic import BaseModel

from silentctx.config import Settings
from silentctx.graph import schema
from silentctx.graph.checks import CheckResult, run_checks
from silentctx.graph.driver import create_driver
from silentctx.logging import get_logger
from silentctx.seed import writer
from silentctx.seed.profiles import Profile
from silentctx.seed.sources import MockData

log = get_logger(__name__)


class SeedRequest(BaseModel):
    network: bool = False
    gold: bool = False
    wipe_gold: bool = False
    profile: Profile = Profile.DEMO

    def validate_rules(self) -> None:
        if not (self.network or self.gold or self.wipe_gold):
            raise ValueError("nothing to do: pass --network, --gold and/or --wipe-gold")
        if self.gold and self.profile is Profile.DEMO:
            raise ValueError(
                "gold is eval-only and never part of the demo profile (--profile full)"
            )
        if self.gold and self.wipe_gold:
            raise ValueError("--gold and --wipe-gold together make no sense")


class SeedResult(BaseModel):
    profile: Profile
    migrations_applied: list[str]
    gold_nodes_wiped: int | None = None
    network: writer.SeedReport | None = None
    gold: writer.SeedReport | None = None
    checks: list[CheckResult]

    @property
    def violations(self) -> int:
        return sum(c.violations for c in self.checks)


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
        result.checks = await run_checks(driver, db)
    finally:
        await driver.close()
    log.info("seed_complete", profile=request.profile, violations=result.violations)
    return result
