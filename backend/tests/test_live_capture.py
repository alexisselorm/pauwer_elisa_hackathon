"""Live checks with the real models from .env (Featherless): ST-01 latency and result on
Elisa's thread, ST-03 on corpus chatter + H-04. Opt-in: uv run pytest -m live -s"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from dotenv import dotenv_values
from neo4j import AsyncDriver

from kaiku.config import Settings
from kaiku.context.models import Scope
from kaiku.extraction.llm import LlmJudge
from kaiku.ingest.store import MessageView, ThreadView
from kaiku.seed.sources import MockData
from tests.capture_helpers import MOCK_DATA, elisa_thread
from tests.test_integration_capture import (  # noqa: F401
    MSG8,
    Rig,
    _thread,
    driver,
    slack_events,
)
from tests.test_integration_neo4j import neo4j_url  # noqa: F401

pytestmark = [pytest.mark.live, pytest.mark.integration]
ENV = dotenv_values(MOCK_DATA.parent / "backend" / ".env")


@pytest.fixture
def judge(make_settings: Callable[..., Settings]) -> LlmJudge:
    if not ENV.get("FEATHERLESS_API_KEY"):
        pytest.skip("FEATHERLESS_API_KEY is not set in backend/.env")
    keys = {
        k.lower(): v
        for k, v in ENV.items()
        if k in ("FEATHERLESS_API_KEY", "CHAT_MODEL", "FAST_MODEL") and v
    }
    return LlmJudge(make_settings(**keys))


async def test_live_elisa_thread_one_card_within_60s(driver: AsyncDriver, judge: LlmJudge) -> None:  # noqa: F811
    """ST-01 / ST-10 / ST-20 with the real models: message 9 to card."""
    rig = Rig(driver, judge)  # type: ignore[arg-type]
    events = slack_events()
    for ev in events[:8]:
        await rig.handlers.on_message(ev)
    start = time.monotonic()
    await rig.handlers.on_message(events[8])  # "Traffic is back 👍"
    results = await rig.loop.sweep_once()
    elapsed = time.monotonic() - start
    print(f"\nmessage 9 -> card: {elapsed:.1f} s; {results[0].status}: {results[0].reason}")
    for item in results[0].items:
        print(
            " item:",
            item.dropped or item.assertion_id,
            item.draft.model_dump_json(include={"avoid"}),
        )
        print(" gates dropped:", item.grounded.dropped if item.grounded else None)
    assert results[0].status == "captured"
    assert elapsed < 60
    assert len(rig.slack.in_channel("CNOC")) == 1
    (aid,) = results[0].assertion_ids
    (a,) = await rig.repo.assertions([aid])
    print(" bindings:", [(b.entity_id, b.scope.value) for b in a.bindings])
    print(" qualifiers:", json.dumps(a.qualifiers)[:500])
    print(" recorded_at:", a.recorded_at, "valid_from:", a.valid_from)
    assert a.qualifiers["action"] == "bounce_interface"
    assert set(a.qualifiers["avoid"]) >= {"replace_sfp", "reboot_node"}
    observed = {b.entity_id for b in a.bindings if b.scope is Scope.OBSERVED_ON}
    applies = {b.entity_id for b in a.bindings if b.scope is Scope.APPLIES_TO}
    assert observed == {"TRN-042"}
    assert applies >= {"TNC-1X", "Gen1"}
    assert a.recorded_at == a.valid_from == MSG8

    for ev in events[9:]:
        await rig.handlers.on_message(ev)
    await rig.loop.sweep_once()
    n = await rig.q("MATCH (x:ContextAssertion) WHERE x.retracted_at IS NULL RETURN count(x) AS n")
    assert n == [{"n": 1}], "messages 10-12 must not add a second rule"
    assert len(rig.slack.in_channel("CNOC")) == 1


async def test_live_triage_and_resolution_ignore_chatter(judge: LlmJudge) -> None:
    """ST-03: corpus chatter (200 messages) + H-04 produce no capture and no resolution."""
    chatter = [
        m
        for m in MockData(MOCK_DATA).teams_messages()
        if m["text"].startswith("generic ops chatter")
    ][:200]
    heldout = [
        json.loads(x)
        for x in (MOCK_DATA.parent / "eval" / "heldout" / "threads.jsonl").open()
        if x.strip()
    ]
    h04 = next(t for t in heldout if t["thread_id"] == "H-04")

    def thread(tid: str, rows: list[dict[str, Any]]) -> ThreadView:
        msgs = [
            MessageView(
                event_id=f"{tid}:{i}",
                event_time=r["ts"],
                author=r["user"],
                author_role=r.get("role"),
                text=r["text"],
            )
            for i, r in enumerate(rows)
        ]
        return ThreadView(
            thread_id=tid, channel="C", last_message_ts=msgs[-1].event_time, messages=msgs
        )

    threads = [
        thread(
            m["message_id"],
            [
                {
                    "ts": m["created_at"],
                    "user": m["author_name"],
                    "role": m["role"],
                    "text": m["text"],
                }
            ],
        )
        for m in chatter
    ]
    threads.append(thread("H-04", h04["messages"]))
    sem = asyncio.Semaphore(4)
    times: list[float] = []

    async def triage(t: ThreadView) -> bool:
        async with sem:
            s = time.monotonic()
            out = await judge.triage(t)
            times.append(time.monotonic() - s)
            assert out.reason
            return out.relevant

    async def resolved(text: str, start: str) -> bool:
        async with sem:
            return await judge.resolves(text, start)

    kept = await asyncio.gather(*(triage(t) for t in threads))
    fired = await asyncio.gather(
        *(resolved(t.messages[-1].text, t.messages[0].text) for t in threads)
    )
    case = elisa_thread().messages
    fix = await judge.resolves("ok it's passing traffic again, kiitos", case[0].text)
    kept_case = await judge.triage(elisa_thread())
    print(
        f"\nkept {sum(kept)}/{len(threads)}, resolution fired {sum(fired)}/{len(threads)}, "
        f"triage {sum(times) / len(times):.2f} s/call (x4 concurrency); "
        f"case kept: {kept_case.relevant}; fix reply resolves: {fix}"
    )
    assert kept_case.relevant
    assert fix, "a real fix reply in a fault thread must resolve it"
    assert not kept[-1], "H-04 must be ignored"
    assert sum(kept) / len(threads) <= 0.1
    assert sum(fired) / len(threads) <= 0.1


def _corpus(conversation: str) -> list[tuple[datetime, str, str, str]]:
    return [
        (datetime.fromisoformat(m["created_at"]), m["author_name"], m["role"], m["text"])
        for m in MockData(MOCK_DATA).teams_messages()
        if m["conversation_id"] == conversation
    ]


KEMI_BARRIER = "19:a6b3a2e9d6e224ea4f96ab06@thread.v2"
KMAX_PAIR = "19:db38376029ab9bc7df60d2f1@thread.v2"
CODE_LOCK_EMAIL = (
    "Kemi site: new code lock installed (action for field ops)\n\nHello,\n\nThe Kemi transport "
    "room gate lock was changed to a code lock during the barrier repair on 3 August. The new "
    "code is handed out by site security. Please update the access register; I have not been "
    "able to access the register so this email is the source.\n\nTimo"
)


async def test_live_corpus_access_supersession_and_dispute(
    driver: AsyncDriver,  # noqa: F811
    judge: LlmJudge,
) -> None:
    """With the real models on corpus text: ST-09a (the Kemi barrier thread is kept as
    access), ST-11 (bound to SEG-042, not a node), ST-22 (the code-lock email supersedes the
    padlock from 3 August) and the KMAX-4K pair (both disputed)."""
    rig = Rig(driver, judge)  # type: ignore[arg-type]

    await _thread(rig, "kemi-barrier", _corpus(KEMI_BARRIER), platform="teams")
    (barrier,) = await rig.loop.sweep_once()
    print(f"\nbarrier: {barrier.status} {barrier.kind}: {barrier.reason}")
    assert barrier.kind == "access"  # ST-09a
    assert barrier.status == "captured"
    (padlock_id,) = barrier.assertion_ids
    (padlock,) = await rig.repo.assertions([padlock_id])
    bound = {b.entity_id for b in padlock.bindings}
    print(" padlock:", padlock.statement, sorted(bound))
    assert "SEG-042" in bound  # ST-11
    assert not any(e.startswith("TRN-") for e in bound)

    sent = datetime(2026, 8, 18, 10, 20, 40, tzinfo=UTC)
    await _thread(
        rig, "kemi-email", [(sent, "Timo Heikkinen", "", CODE_LOCK_EMAIL)], platform="email"
    )
    (email,) = await rig.loop.sweep_once()
    (code_id,) = email.assertion_ids
    old, new = await rig.repo.assertions([padlock_id, code_id])
    print(" code lock:", new.statement, new.valid_from, "| padlock:", old.status, old.valid_to)
    aug3 = datetime(2026, 8, 2, 21, tzinfo=UTC)
    assert new.valid_from == aug3  # ST-22
    assert new.recorded_at == sent
    assert old.status == "superseded"
    assert old.valid_to == aug3

    await _thread(rig, "kmax", _corpus(KMAX_PAIR), platform="teams")
    (kmax,) = await rig.loop.sweep_once()
    views = await rig.repo.assertions(kmax.assertion_ids)
    print(" kmax:", [(v.statement[:60], v.status, v.qualifiers.get("action")) for v in views])
    restart = [v for v in views if v.qualifiers.get("action") == "power_cycle_radio"]
    assert restart, "the power-cycle rule is captured"
    assert all(v.status == "disputed" for v in restart)
    assert any(v.contradicts for v in views)
