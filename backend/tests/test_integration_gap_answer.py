"""A knowledge gap asked in Slack is answered by the expert's reply, end to end: [Ask] posts
the question, the reply in that thread is captured like any thread, the extracted assertion
answers the gap in the same transaction, and the asker's message shows the answer.
Real Neo4j (testcontainers), fake Slack, fake LLM. Run: uv run pytest -m integration"""

# ruff: noqa: PT009  # `assert_` is the repository method, not unittest

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from neo4j import AsyncDriver

from kaiku.agents.ops_agent import AnswerStatus, OpsAnswer
from kaiku.context.models import AssertionIn, BindingIn, Scope, SourceIn
from kaiku.domain.models import Platform
from kaiku.domain.time import slack_ts_to_datetime
from kaiku.errors import WriteRejectedError
from kaiku.extraction.models import Mention, Quoted, RuleDraft, TriageKind
from kaiku.ingest.store import MessageIn
from kaiku.slack import actions as a
from kaiku.slack import capture as cap
from tests.capture_helpers import FakeJudge
from tests.test_integration_capture import FakeSlack, Rig, driver  # noqa: F401
from tests.test_integration_neo4j import neo4j_url  # noqa: F401  # shared container

pytestmark = pytest.mark.integration

QUESTION = "How do I get through the Kemi gate at night?"
REPLY = (
    "At night the Kemi gate stays locked: call site security on the gate intercom, "
    "they open it remotely."
)
ASKER_CHANNEL, ASKER_MSG = "CASK", "1799999000.000100"  # the agent's "not documented" reply
GAP_CHANNEL = "CGAP"
EXPERT = "U0ERKKI"


class Slack(FakeSlack):
    async def chat_getPermalink(self, **kw: Any) -> dict[str, Any]:
        return {"permalink": f"https://elisa.slack.com/archives/{kw['channel']}/{kw['message_ts']}"}

    async def users_list(self, **kw: Any) -> dict[str, Any]:
        return {"members": [{"id": EXPERT, "name": "erkki", "profile": {"real_name": "Erkki"}}]}


class _Unused:
    async def ask(self, *args: Any, **kw: Any) -> OpsAnswer:
        raise AssertionError("not asked")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no tool call")


async def _ack() -> None:
    return None


def _night_access_rule() -> RuleDraft:
    return RuleDraft(
        statement="Out of hours, site security opens the Kemi gate remotely via the intercom.",
        topic="access_keys",
        action=Quoted(value="call_site_security", quote="call site security on the gate intercom"),
        entities=[Mention(surface_form="Kemi gate")],
    )


async def _open_gap(rig: Rig, question: str = QUESTION) -> str:
    out, _ = await rig.repo.open_gap(
        agent_id="ops-agent",
        idempotency_key=f"t-{uuid.uuid4().hex}",
        now=datetime.now(UTC),
        question=question,
        entity_ids=["SEG-042"],
        context=None,
        routed_to="erkki",
    )
    return str(out["gap_id"])


async def _fresh(rig: Rig) -> None:
    await rig.q("MATCH (g:KnowledgeGap) DETACH DELETE g")


async def test_the_experts_reply_answers_the_gap_and_the_asker_sees_it(
    driver: AsyncDriver,  # noqa: F811
) -> None:
    rig = Rig(driver, FakeJudge([_night_access_rule()], kind=TriageKind.ACCESS))
    await _fresh(rig)
    slack = Slack()
    rig.cards.client = slack  # type: ignore[assignment]
    # Capture is narrowed to the work channel; the gap channel is not in it.
    rig.ingest.channels = {"CNOC"}
    gap_id = await _open_gap(rig)

    # The agent answered "not documented" with [Ask]; the asker clicks it.
    answer = OpsAnswer(
        status=AnswerStatus.NOT_DOCUMENTED,
        summary="I found no record of this.",
        gap_id=gap_id,
        gap_routed_to="erkki",
    )
    blocks = a.answer_blocks(answer, question=QUESTION)
    [value] = [e["value"] for b in blocks if b["type"] == "actions" for e in b["elements"][:1]]
    ops = a.OpsActions(
        _Unused(),
        _Unused(),
        gap_channel=GAP_CHANNEL,
        gaps=cap.GapAskRecorder(rig.repo, rig.ingest),
    )
    click = {
        "actions": [{"action_id": a.GAP_ASK, "value": value}],
        "channel": {"id": ASKER_CHANNEL},
        "message": {"ts": ASKER_MSG, "blocks": blocks},
        "user": {"id": "U0ASKER"},
    }
    await ops.on_gap(_ack, click, slack)  # type: ignore[arg-type]

    [ask] = slack.in_channel(GAP_CHANNEL)
    assert ask["text"].startswith(f":question: <@{EXPERT}>, <@U0ASKER> asked")
    ask_ts = "1800000000.000001"
    gap = await rig.repo.gap(gap_id)
    assert gap is not None
    assert (gap["ask_channel"], gap["ask_ts"], gap["status"]) == (GAP_CHANNEL, ask_ts, "open")
    assert gap["asker_messages"] == [f"{ASKER_CHANNEL}:{ASKER_MSG}"]

    # The question alone is not triaged: no LLM call until the expert replies.
    assert await rig.loop.sweep_once() == []
    assert rig.judge.calls == []

    # The expert replies in the thread. Nothing else: no ✅, no form, no resolution word.
    reply_ts = "1800000100.000200"
    await rig.handlers.on_message(
        {
            "type": "message",
            "channel": GAP_CHANNEL,
            "user": EXPERT,
            "ts": reply_ts,
            "thread_ts": ask_ts,
            "text": REPLY,
        }
    )
    assert rig.loop._kick.is_set()  # read now, not after the idle timeout
    [result] = await rig.loop.sweep_once()
    assert result.status == "captured", result
    [aid] = result.assertion_ids

    # The thread reads question -> answer.
    thread = await rig.store.thread(f"slack:{GAP_CHANNEL}:{ask_ts}")
    assert [(m.author, m.text) for m in thread.messages] == [
        ("U0ASKER", QUESTION),
        (EXPERT, REPLY),
    ]

    # The gap is answered by that assertion, as of the reply.
    gap = await rig.repo.gap(gap_id)
    assert gap is not None
    assert gap["status"] == "answered"
    assert gap["answered_by"] == [aid]
    assert gap["answered_at"] == slack_ts_to_datetime(reply_ts)
    (view,) = await rig.repo.assertions([aid])
    assert "SEG-042" in {b.entity_id for b in view.bindings}

    # One context card in the ask thread, like any captured thread...
    [card] = [p for p in slack.in_channel(GAP_CHANNEL) if p.get("thread_ts") == ask_ts]
    assert card["text"].startswith("Captured")
    # ...and the asker's message, edited in place, now carries the answer.
    noted, told = [u for u in slack.updated if u["channel"] == ASKER_CHANNEL]
    assert noted["text"].startswith(f"Asked <@{EXPERT}>")  # at the click
    assert noted["ts"] == told["ts"] == ASKER_MSG
    assert told["text"] == f"Answered by <@{EXPERT}>: {view.statement}"
    assert gap_id in str(told["blocks"])
    assert f"archives/{GAP_CHANNEL}/{ask_ts}" in str(told["blocks"])
    assert QUESTION in str(told["blocks"])

    # Asked again now, the gap is not reposted: the thread already has the answer.
    again = Slack()
    await ops.on_gap(_ack, click, again)  # type: ignore[arg-type]
    assert again.posted == []
    assert again.updated[0]["text"].startswith("Already answered")


async def test_the_same_question_opens_one_gap_until_it_is_answered(
    driver: AsyncDriver,  # noqa: F811
) -> None:
    """Asked twice before the answer (different requests, other wording of case and
    punctuation): one gap. Once answered, a new question is a new gap."""
    rig = Rig(driver, FakeJudge())
    await _fresh(rig)
    first = await _open_gap(rig)
    second = await _open_gap(rig, "how do I get through the KEMI gate at night")
    assert second == first
    other = await _open_gap(rig, "Who has the key to the Pori shelter?")
    assert other != first
    rows = await rig.q("MATCH (g:KnowledgeGap) RETURN count(g) AS n")
    assert rows == [{"n": 2}]

    aid = await _assertion(rig, "kemi-night-1")
    await rig.repo.answer_gap(first, aid)
    assert await _open_gap(rig) != first


async def _assertion(
    rig: Rig, event_id: str, *, at: datetime = datetime(2026, 10, 1, 8, tzinfo=UTC)
) -> str:
    """A message, and an assertion derived from it."""
    await rig.store.store(
        [
            MessageIn(
                event_id=event_id,
                platform=Platform.SLACK,
                channel="C9",
                thread_id=event_id,
                event_time=at,
                author="U0ERKKI",
                text=REPLY,
            )
        ],
        at,
    )
    out = await rig.repo.assert_(_item(event_id, "SEG-042", None))
    return out.assertion_id


def _item(event_id: str, entity_id: str, answers_gap: str | None) -> AssertionIn:
    return AssertionIn(
        statement="Site security opens the Kemi gate remotely at night.",
        topic="access_keys",
        bindings=[BindingIn(entity_id=entity_id, scope=Scope.OBSERVED_ON)],
        sources=[SourceIn(event_id=event_id)],
        answers_gap=answers_gap,
    )


async def test_the_gap_closes_in_the_assertions_transaction(
    driver: AsyncDriver,  # noqa: F811
) -> None:
    """A rejected write leaves the gap open; a replay of knowledge already on record still
    answers it."""
    rig = Rig(driver, FakeJudge())
    await _fresh(rig)
    gap_id = await _open_gap(rig)
    wrong = await _assertion(rig, "kemi-night-2")
    # A fix whose source is later than the correction: the fix is written (and links the
    # gap) before the R2 check rejects it, so only a rollback can leave the gap open.
    await _assertion(rig, "kemi-night-2-fix", at=datetime(2026, 10, 3, 8, tzinfo=UTC))
    await _assertion(rig, "kemi-night-2-click", at=datetime(2026, 10, 2, 8, tzinfo=UTC))
    with pytest.raises(WriteRejectedError, match="R2"):
        await rig.repo.correct(
            wrong,
            _item("kemi-night-2-fix", "SEG-042", gap_id).model_copy(
                update={"statement": "Site security opens the gate."}
            ),
            "kemi-night-2-click",
        )
    gap = await rig.repo.gap(gap_id)
    assert gap is not None
    assert (gap["status"], gap["answered_by"]) == ("open", [])

    out = await rig.repo.assert_(_item("kemi-night-2", "SEG-042", gap_id))
    assert out.replayed
    gap = await rig.repo.gap(gap_id)
    assert gap is not None
    assert (gap["status"], gap["answered_by"]) == ("answered", [out.assertion_id])
