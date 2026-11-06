"""The slackbot and simulator processes (no Slack connection needed)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from silentctx.agents.ops_agent import AnswerStatus, OpsAnswer, Step
from silentctx.config import Settings
from silentctx.sim.app import create_app as create_sim_app
from silentctx.slack import bot


def test_sim_health() -> None:
    with TestClient(create_sim_app()) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert len(r.headers["x-request-id"]) == 32


async def test_slackbot_without_tokens_idles_instead_of_crashing(
    make_settings: Callable[..., Settings],
) -> None:
    task = asyncio.create_task(bot.run(make_settings()))
    await asyncio.sleep(0.1)
    assert not task.done()  # still running: no crash-loop under `restart: unless-stopped`
    assert not bot.READY_FILE.exists()  # healthcheck stays red
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class _Client:
    """Records chat.postMessage / chat.update calls like Slack's AsyncWebClient."""

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ts": f"9.{len(self.posted):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updated.append(kwargs)
        return {"ok": True}


class _Answerer:
    def __init__(self, fail: bool = False) -> None:
        self.asked: list[tuple[str, str, str | None]] = []
        self.fail = fail

    async def ask(self, question: str, *, request_id: str, asker: str | None = None) -> OpsAnswer:
        self.asked.append((question, request_id, asker))
        if self.fail:
            raise TimeoutError("featherless slow")
        return OpsAnswer(
            status=AnswerStatus.ANSWERED,
            summary="Bounce the port.",
            steps=[Step(action="Disable and re-enable 1/1/3", assertion_ids=["ASR-0001"])],
            avoid=["replace_sfp"],
        )


MENTION = {
    "user": "U1",
    "channel": "C1",
    "ts": "1.000001",
    "text": "<@UBOT> I swapped the SFP on the Pori node, no traffic",
}


async def test_mention_is_answered_by_editing_one_placeholder_in_the_thread() -> None:
    client, answerer = _Client(), _Answerer()
    await bot.SlackOps(answerer).handle_mention(MENTION, client)  # type: ignore[arg-type]
    assert answerer.asked == [
        ("I swapped the SFP on the Pori node, no traffic", "slack:C1:1.000001", "<@U1>")
    ]
    assert client.posted == [{"channel": "C1", "thread_ts": "1.000001", "text": bot.THINKING}]
    assert len(client.updated) == 1  # one bot message per question, edited in place
    update = client.updated[0]
    assert update["ts"] == "9.000001"
    assert "Disable and re-enable 1/1/3" in update["text"]
    assert "ASR-0001" in update["text"]


async def test_mention_inside_a_thread_replies_in_that_thread() -> None:
    client = _Client()
    event = {**MENTION, "ts": "2.000001", "thread_ts": "1.000001"}
    await bot.SlackOps(_Answerer()).handle_mention(event, client)  # type: ignore[arg-type]
    assert client.posted[0]["thread_ts"] == "1.000001"


async def test_empty_mention_gets_help_not_an_agent_call() -> None:
    client, answerer = _Client(), _Answerer()
    await bot.SlackOps(answerer).handle_mention({**MENTION, "text": "<@UBOT>"}, client)  # type: ignore[arg-type]
    assert answerer.asked == []
    assert "Ask me about the network" in client.posted[0]["text"]


async def test_agent_failure_still_replies() -> None:
    client = _Client()
    await bot.SlackOps(_Answerer(fail=True)).handle_mention(MENTION, client)  # type: ignore[arg-type]
    assert "couldn't answer" in client.updated[0]["text"]
    assert "TimeoutError" in client.updated[0]["text"]


async def test_dm_is_answered_channel_message_is_only_logged() -> None:
    client, answerer = _Client(), _Answerer()
    ops = bot.SlackOps(answerer)
    dm = {"user": "U1", "channel": "D1", "channel_type": "im", "ts": "3.0", "text": "Kemi gate?"}
    await ops.handle_message(dm, client)  # type: ignore[arg-type]
    chan = {**dm, "channel": "C1", "channel_type": "channel"}
    await ops.handle_message(chan, client)  # type: ignore[arg-type]
    assert [q for q, _, _ in answerer.asked] == ["Kemi gate?"]
    assert client.posted[0]["thread_ts"] is None


@pytest.mark.parametrize(
    "event",
    [
        {"bot_id": "B1", "channel_type": "im", "channel": "D1", "text": "x"},
        {"subtype": "bot_message", "channel_type": "im", "channel": "D1", "text": "x"},
        {"subtype": "message_changed", "channel_type": "im", "channel": "D1", "text": "x"},
    ],
)
async def test_bots_and_edits_are_ignored(event: dict[str, Any]) -> None:
    client, answerer = _Client(), _Answerer()
    ops = bot.SlackOps(answerer)
    await ops.handle_message(event, client)  # type: ignore[arg-type]
    await ops.handle_mention(event, client)  # type: ignore[arg-type]
    assert client.posted == []
    assert answerer.asked == []


def test_question_text_strips_mentions() -> None:
    event = {"text": "<@U0BOT|silentctx>  what about   <@U123> TRN-017?"}
    assert bot.question_text(event) == "what about TRN-017?"


def test_app_registers_both_handlers(make_settings: Callable[..., Settings]) -> None:
    app = bot.build_app(make_settings(slack_bot_token="xoxb-test"), answerer=_Answerer())
    names = {listener.ack_function.__name__ for listener in app._async_listeners}
    assert names == {"handle_mention", "handle_message"}
