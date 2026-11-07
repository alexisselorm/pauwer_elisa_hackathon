"""Stale KB pages through the real capture loop and Neo4j: they come in through the
connectors with low authority, and never outrank expert chat or email, whichever arrives
first. The LLM is a fake; everything else is the real code. Run: uv run pytest -m integration
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from neo4j import AsyncDriver

from kaiku.context.models import Match, Scope
from kaiku.extraction.models import Mention, RuleDraft
from kaiku.ingest.itsm_kb import kb_message, read_articles
from kaiku.ingest.store import MessageIn
from tests.capture_helpers import ELISA_RULE, MOCK_DATA, FakeJudge, q
from tests.test_integration_capture import Rig, _capture_elisa, driver  # noqa: F401
from tests.test_integration_neo4j import neo4j_url  # noqa: F401  # shared container

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 7, tzinfo=UTC)
KB_RULE = RuleDraft(
    statement="ITSM KB: on TRN-042, if there is no traffic after an SFP replacement, reboot "
    "the whole node.",
    topic="forwarding_state_after_sfp_swap",
    action=q("reboot_node", "reboot the whole node"),
    entities=[Mention(surface_form="TRN-042", role=Scope.OBSERVED_ON)],
)


def _stale_kb() -> MessageIn:
    articles, _, _ = read_articles(MOCK_DATA)
    return next(a for a in articles if a.event_id == "itsm:KB0017001")


async def _capture_kb(rig: Rig, article: MessageIn) -> str:
    rig.judge.rules = [KB_RULE]
    await rig.store.store([article], NOW)
    (result,) = await rig.loop.sweep_once()
    assert result.status == "captured", result
    (aid,) = result.assertion_ids
    return aid


async def test_backfilled_kb_is_disputed_by_the_expert_and_ranks_below_it(
    driver: AsyncDriver,  # noqa: F811
) -> None:
    rig = Rig(driver, FakeJudge())
    kb = await _capture_kb(rig, _stale_kb())
    stored = await rig.q(
        "MATCH (a:ContextAssertion {assertion_id: $id})-[:DERIVED_FROM]->(e:Event) "
        "RETURN a.authority AS a, a.valid_from AS vf, e.authority AS e, e.platform AS p",
        id=kb,
    )
    assert [(r["a"], r["e"], r["p"]) for r in stored] == [(0, "low", "itsm")]
    assert stored[0]["vf"].to_native() == datetime(2024, 3, 2, tzinfo=UTC)  # published

    rig.judge.rules = [ELISA_RULE]
    rig.judge.relations = {kb: "contradicts"}
    expert = await _capture_elisa(rig)
    e, k = await rig.repo.assertions([expert, kb])
    assert (e.status, k.status) == ("unverified", "disputed")
    assert e.contradicts == [kb]

    ranked = await rig.repo.context({"TRN-042": Match.DIRECT}, NOW, NOW)
    order = [a.assertion_id for a in ranked if a.assertion_id in (expert, kb)]
    assert order == [expert, kb]
    assert (ranked[0].score or 0) > (ranked[1].score or 0)


async def test_a_kb_published_after_the_expert_does_not_supersede_or_dispute_it(
    driver: AsyncDriver,  # noqa: F811
) -> None:
    rig = Rig(driver, FakeJudge())
    expert = await _capture_elisa(rig)
    # The same stale workaround, republished months after the expert's rule.
    republished = kb_message(
        {
            "article_id": "KB0017001-R2",
            "title": "TRN-042 known issue: no traffic after SFP replacement",
            "body": "Known issue on TRN-042 only. Workaround: reboot the whole node.",
            "valid_from": "2026-01-10",
            "status": "published",
        }
    )
    rig.judge.relations = {expert: "supersedes"}
    kb = await _capture_kb(rig, republished)
    e, k = await rig.repo.assertions([expert, kb])
    assert e.status == "unverified"
    assert e.valid_to is None  # not superseded (R4b needs the authority to do it)
    assert k.status == "disputed"
    assert k.contradicts == [expert]
    assert await rig.q("MATCH (:ContextAssertion)-[s:SUPERSEDES]->() RETURN count(s) AS n") == [
        {"n": 0}
    ]
