"""The slackbot and simulator processes (no Slack connection needed)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

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


class _Say:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


async def test_mention_gets_a_hello_in_thread() -> None:
    say = _Say()
    await bot.handle_mention({"user": "U1", "channel": "C1", "ts": "1.000001"}, say)  # type: ignore[arg-type]
    assert say.calls == [
        {"text": "Hello <@U1>! :wave: silentctx is online.", "thread_ts": "1.000001"}
    ]


async def test_mention_inside_a_thread_replies_in_that_thread() -> None:
    say = _Say()
    event = {"user": "U1", "channel": "C1", "ts": "2.000001", "thread_ts": "1.000001"}
    await bot.handle_mention(event, say)  # type: ignore[arg-type]
    assert say.calls[0]["thread_ts"] == "1.000001"


async def test_dm_gets_a_hello_channel_message_does_not() -> None:
    say = _Say()
    await bot.handle_message({"user": "U1", "channel": "D1", "channel_type": "im"}, say)  # type: ignore[arg-type]
    await bot.handle_message({"user": "U1", "channel": "C1", "channel_type": "channel"}, say)  # type: ignore[arg-type]
    assert len(say.calls) == 1


@pytest.mark.parametrize(
    "event",
    [
        {"bot_id": "B1", "channel_type": "im"},
        {"subtype": "bot_message", "channel_type": "im"},
        {"subtype": "message_changed", "channel_type": "im"},
    ],
)
async def test_bots_and_edits_are_ignored(event: dict[str, object]) -> None:
    say = _Say()
    await bot.handle_message(event, say)  # type: ignore[arg-type]
    await bot.handle_mention(event, say)  # type: ignore[arg-type]
    assert say.calls == []


def test_app_registers_both_handlers(make_settings: Callable[..., Settings]) -> None:
    app = bot.build_app(make_settings(slack_bot_token="xoxb-test"))
    names = {listener.ack_function.__name__ for listener in app._async_listeners}
    assert names == {"handle_mention", "handle_message"}
