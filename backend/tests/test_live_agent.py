"""The ops agent with the real chat model, over the real MCP server (BE-4.3): ST-30 from
live-captured knowledge only, ST-35 not documented, ST-36 control questions.
Opt-in (costs tokens): uv run pytest -m live -s tests/test_live_agent.py"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Iterator

import httpx
import pytest
from neo4j import AsyncDriver

from silentctx.agents.ops_agent import AnswerStatus, OpsAgent, OpsAnswer
from silentctx.config import Settings
from silentctx.extraction.llm import LlmJudge
from silentctx.main import create_app
from silentctx.seed.profiles import Profile
from silentctx.seed.service import SeedRequest, seed
from silentctx.slack.actions import SimNetwork, fault_question
from tests.capture_helpers import MOCK_DATA
from tests.test_integration_capture import CHANNEL, PASSWORD, Rig, driver, slack_events  # noqa: F401
from tests.test_integration_neo4j import _serve, _sim_server, _SimClock, neo4j_url  # noqa: F401
from tests.test_live_capture import ENV, judge  # noqa: F401

pytestmark = [pytest.mark.live, pytest.mark.integration]
LLM_KEYS = ("FEATHERLESS_API_KEY", "CHAT_MODEL", "FAST_MODEL")


@pytest.fixture(autouse=True)
def _fresh_llm_client() -> None:
    """langchain-openai caches one async HTTP client per process, bound to the event loop
    that first used it; each test here runs on its own loop."""
    from langchain_openai.chat_models import _client_utils  # noqa: TID251  # test-only

    _client_utils._cached_async_httpx_client.cache_clear()


@contextlib.contextmanager
def _agent(
    make_settings: Callable[..., Settings], neo4j: str, sim_url: str | None = None
) -> Iterator[OpsAgent]:
    keys = {k.lower(): v for k, v in ENV.items() if k in LLM_KEYS and v}
    extra = {"sim_url": sim_url} if sim_url else {}
    api = make_settings(
        neo4j_uri=neo4j, neo4j_password=PASSWORD, data_dir=MOCK_DATA, **keys, **extra
    )
    with _serve(create_app(api)) as url:
        deadline = time.monotonic() + 60
        while httpx.get(f"{url}/readyz").status_code != 200:
            assert time.monotonic() < deadline, "api never became ready"
            time.sleep(0.2)
        yield OpsAgent(make_settings(mcp_url=f"{url}/mcp", **keys))


def _show(qid: str, answer: OpsAnswer) -> None:
    print(f"\n{qid}: {answer.status} - {answer.summary}")
    print(" ", answer.model_dump_json(include={"steps", "avoid", "conditions", "sources"})[:800])


async def test_live_st30_fault_answered_from_the_captured_thread(
    driver: AsyncDriver,  # noqa: F811
    judge: LlmJudge,  # noqa: F811
    neo4j_url: str,  # noqa: F811
    make_settings: Callable[..., Settings],
) -> None:
    """ST-30: knowledge from ST-01 only (no gold). A stall on TRN-017 1/1/2 (TNC-1X) gets
    conditions met x3, bounce as the step, SFP swap and reboot to avoid, and the permalink
    to Erkki's message 8 as the source."""
    rig = Rig(driver, judge)  # type: ignore[arg-type]
    events = slack_events()
    for ev in events[:9]:
        await rig.handlers.on_message(ev)
    [captured] = await rig.loop.sweep_once()
    assert captured.status == "captured", captured.reason
    gold = await rig.q("MATCH (a:ContextAssertion {origin: 'gold'}) RETURN count(a) AS n")
    assert gold == [{"n": 0}]
    permalink = f"https://elisa.slack.com/archives/{CHANNEL}/p{events[7]['ts'].replace('.', '')}"

    clock = _SimClock()
    base = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA)
    with _sim_server(clock, base) as sim_url:
        sim = SimNetwork(sim_url)
        await sim._request(
            "POST",
            "/sim/fault",
            json={"node": "TRN-017", "port": "1/1/2", "kind": "sfp_swap_stall"},
        )
        trigger = [e for e in await sim.events() if e["type"] == "OPTICAL_POWER_OK"][-1]
        with _agent(make_settings, neo4j_url, sim_url) as agent:
            question = fault_question(trigger)  # as the proactive post asks it
            answer = await agent.ask(
                question, request_id="live-st30", trigger_event_id=trigger["event_id"]
            )
    _show("ST-30", answer)
    (aid,) = captured.assertion_ids
    assert answer.status is AnswerStatus.ANSWERED
    checked = {c.name: c.status for c in answer.conditions if c.assertion_id == aid}
    telemetry = ("oper_up", "rx_power_normal", "counters_zero")
    assert [checked.get(n) for n in telemetry] == ["met"] * 3, checked
    # Anything else the extraction listed has no telemetry check: stated, never claimed met.
    assert all(v != "met" for n, v in checked.items() if n not in telemetry), checked
    bounce = ("bounce", "disable", "shut")
    assert any(
        aid in s.assertion_ids and any(w in s.action.lower() for w in bounce) for s in answer.steps
    ), answer.steps
    avoid = " ".join(answer.avoid).lower()
    assert "sfp" in avoid
    assert "reboot" in avoid
    links = [(s.link or "").split("?")[0] for s in answer.sources if s.assertion_id == aid]
    assert permalink in links, links  # Erkki's message 8, not the thread root


def _questions() -> list[tuple[str, str]]:
    key = [json.loads(line) for line in (MOCK_DATA / "answer_key" / "questions.jsonl").open()]
    heldout = MOCK_DATA.parent / "eval" / "heldout" / "questions.jsonl"
    held = [json.loads(line) for line in heldout.open()]
    return [
        (q["id"], q["question"])
        for q in key
        if not q["in_corpus"] and not q.get("from_network_data")
    ] + [(q["id"], q["ask"]) for q in held if q["status"] == "not_documented"]


async def _ask_all(
    agent: OpsAgent, items: list[tuple[str, str]]
) -> dict[str, OpsAnswer | Exception]:
    gate = asyncio.Semaphore(1)  # one at a time: with 3 in parallel QA-034/035 timed out

    async def one(qid: str, text: str) -> tuple[str, OpsAnswer | Exception]:
        async with gate:
            try:
                return qid, await agent.ask(text, request_id=f"live-{qid}")
            except Exception as exc:  # noqa: BLE001  # a failed run is reported, not hidden
                return qid, exc

    return dict(await asyncio.gather(*(one(q, t) for q, t in items)))


async def test_live_st35_st36_documented_or_not(
    driver: AsyncDriver,  # noqa: F811
    neo4j_url: str,  # noqa: F811
    make_settings: Callable[..., Settings],
) -> None:
    """With the full gold graph seeded: ST-35, every not_documented question -> not
    documented, no steps, a gap opened (0 confident answers); ST-36, QA-001/002 answered
    from the network data, not refused."""
    settings = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA)
    assert (await seed(settings, SeedRequest(gold=True, profile=Profile.FULL))).violations == 0
    try:
        not_documented = _questions()
        assert len(not_documented) == 10  # 7 from the answer key + HQ-10..12
        control = [
            ("QA-001", "How many transport nodes use the Gen1 hardware generation?"),
            ("QA-002", "Which city hosts transmission node TRN-042?"),
        ]
        with _agent(make_settings, neo4j_url) as agent:
            answers = await _ask_all(agent, not_documented + control)
        failed = {q: repr(a)[:200] for q, a in answers.items() if isinstance(a, Exception)}
        for qid, answer in answers.items():
            if not isinstance(answer, Exception):
                _show(qid, answer)
        assert failed == {}, failed
        done = {q: a for q, a in answers.items() if isinstance(a, OpsAnswer)}

        confident = {
            q: a.status
            for q, _ in not_documented
            if (a := answers[q]).status is not (AnswerStatus.NOT_DOCUMENTED)
        }
        assert confident == {}, "ST-35: confident answers to undocumented questions"
        for qid, _ in not_documented:
            assert done[qid].steps == [], qid
            assert done[qid].gap_id, f"{qid}: no knowledge gap opened"

        qa1, qa2 = done["QA-001"], done["QA-002"]
        assert qa1.status is not AnswerStatus.NOT_DOCUMENTED
        assert "6" in qa1.summary  # Gen1 nodes in service now (9 before May-July 2026)
        assert qa2.status is not AnswerStatus.NOT_DOCUMENTED
        assert "kemi" in qa2.summary.lower()
    finally:
        await seed(settings, SeedRequest(wipe_gold=True, profile=Profile.FULL))
