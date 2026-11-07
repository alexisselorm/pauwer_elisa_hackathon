"""The demo baseline (plan 001 §3.5): the console's Reset and `kaiku seed --gold` with the
demo profile seed the gold knowledge minus the forwarding-stall assertions. No Neo4j."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kaiku.seed import reset as reset_mod
from kaiku.seed import service
from kaiku.seed.profiles import Profile, demo_exclusions
from kaiku.seed.sources import MockData
from kaiku.seed.writer import SeedReport

MOCK = Path(__file__).parents[2] / "mock_data"


class Driver:
    """Records every query; answers each with the row it was told to."""

    def __init__(self, counts: list[int] | None = None) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.counts = list(counts or [])
        self.closed = False

    async def execute_query(self, query: str, params: Any = None, **kw: Any) -> Any:
        kw.pop("database_", None)
        self.queries.append((query, {**(params or {}), **kw}))
        n = self.counts.pop(0) if self.counts else 0
        return [{"n": n}], None, None

    async def close(self) -> None:
        self.closed = True


def test_gold_is_allowed_with_the_demo_profile() -> None:
    service.SeedRequest(network=True, gold=True, profile=Profile.DEMO).validate_rules()
    demo = service.SeedRequest(network=True, gold=True, profile=Profile.DEMO)
    assert demo == reset_mod.DEMO_SEED


def test_the_stale_kb_page_is_believed_again_without_its_successor() -> None:
    data = MockData(MOCK)
    excluded = demo_exclusions(data).gold_assertion_ids
    # ASR-0030 (ITSM KB: reboot the node) was superseded by ASR-0001, Elisa's thread.
    assert service.reopened_by_exclusion(data, excluded) == ["ASR-0030"]
    assert service.reopened_by_exclusion(data, set()) == []


async def test_exclusion_deletes_the_stall_gold_and_reopens_its_predecessor() -> None:
    data = MockData(MOCK)
    driver = Driver()
    removed = await service.exclude_demo_gold(driver, "neo4j", data)  # type: ignore[arg-type]
    assert set(removed) == demo_exclusions(data).gold_assertion_ids
    assert "ASR-0001" in removed
    delete, reopen = driver.queries
    assert "DETACH DELETE" in delete[0]
    assert delete[1]["ids"] == removed
    assert "believed_true" in reopen[0]
    assert reopen[1]["ids"] == ["ASR-0030"]


def test_result_counts_the_gold_left_in_the_graph() -> None:
    result = service.SeedResult(
        profile=Profile.DEMO,
        migrations_applied=[],
        gold=SeedReport(gold_assertions=24),
        gold_excluded=["ASR-0001", "ASR-0002"],
        checks=[],
    )
    assert result.gold_assertions == 22
    assert (
        service.SeedResult(profile=Profile.FULL, migrations_applied=[], checks=[]).gold_assertions
        == 0
    )


async def test_reset_wipes_in_batches_then_seeds_the_demo_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = Driver(counts=[reset_mod.BATCH, 7])
    seeded: list[service.SeedRequest] = []

    async def fake_seed(settings: Any, request: service.SeedRequest) -> service.SeedResult:
        seeded.append(request)
        return service.SeedResult(profile=request.profile, migrations_applied=[], checks=[])

    monkeypatch.setattr(reset_mod, "create_driver", lambda settings: driver)
    monkeypatch.setattr(reset_mod, "seed", fake_seed)
    settings = SimpleNamespace(neo4j_database="neo4j")
    out = await reset_mod.reset(settings)  # type: ignore[arg-type]
    assert out.deleted_nodes == reset_mod.BATCH + 7
    assert len(driver.queries) == 2
    assert driver.closed
    assert "__Neo4jMigration" in driver.queries[0][0]  # the schema and cursors are kept
    assert seeded == [reset_mod.DEMO_SEED]
