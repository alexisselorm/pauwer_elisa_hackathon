"""`silentctx-slackbot`: the Bolt app in Socket Mode (no public URL needed).

Answers questions with the ops agent (@mention in a channel, or a DM), and logs that channel
messages arrive (capture is SL-1). The agent reaches the context layer through the API's
MCP server only, never by importing it (plan 002).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Protocol

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from silentctx.agents.ops_agent import OpsAgent, OpsAnswer
from silentctx.config import Settings, get_settings
from silentctx.logging import configure_logging, get_logger
from silentctx.slack.format import format_answer

# Written once connected; the compose healthcheck looks for it.
READY_FILE = Path("/tmp/silentctx-slackbot.ready")  # noqa: S108  # container-local marker
log = get_logger(__name__)


class Answerer(Protocol):
    async def ask(
        self, question: str, *, request_id: str, asker: str | None = None
    ) -> OpsAnswer: ...


THINKING = ":hourglass_flowing_sand: Looking into it…"
HELP = (
    "Hi <@{user}>! :wave: Ask me about the network in plain words, e.g. "
    '_"I swapped the SFP on the Pori node and there\'s no traffic, what should I try?"_ '
    'or _"How do I get through the gate at the Kemi site?"_'
)
_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")


def _is_from_a_bot(event: dict[str, Any]) -> bool:
    # Never answer bots (ourselves included): that is how reply loops start.
    return bool(event.get("bot_id")) or event.get("subtype") == "bot_message"


def question_text(event: dict[str, Any]) -> str:
    return " ".join(_MENTION.sub(" ", event.get("text") or "").split())


class SlackOps:
    """The ops agent's Slack surface: @mentions in channels and DMs (plan 002 §3.4).
    One bot message per question: a placeholder, edited in place into the answer."""

    def __init__(self, answerer: Answerer) -> None:
        self.answerer = answerer

    async def answer(
        self, client: AsyncWebClient, event: dict[str, Any], *, thread_ts: str | None
    ) -> None:
        channel, user = event["channel"], event.get("user")
        question = question_text(event)
        if not question:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=HELP.format(user=user)
            )
            return
        placeholder = await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=THINKING
        )
        try:
            result = await self.answerer.ask(
                question, request_id=f"slack:{channel}:{event.get('ts')}", asker=f"<@{user}>"
            )
            text = format_answer(result)
        except Exception as exc:  # the user always gets a reply
            log.exception("ops_agent_failed", channel=channel, ts=event.get("ts"))
            text = (
                ":warning: Sorry, I couldn't answer that right now "
                f"({type(exc).__name__}). Please try again in a minute."
            )
        await client.chat_update(channel=channel, ts=placeholder["ts"], text=text)

    async def handle_mention(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        """@silentctx in a channel: answer in the thread of the mention."""
        if _is_from_a_bot(event) or event.get("subtype"):
            return
        log.info("slack_mention", channel=event.get("channel"), ts=event.get("ts"))
        await self.answer(client, event, thread_ts=event.get("thread_ts") or event.get("ts"))

    async def handle_message(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        """A DM to the bot is a question. Channel messages are only logged for now (capture
        is SL-1); an @mention in a channel is answered by `handle_mention`, not here."""
        if _is_from_a_bot(event) or event.get("subtype"):  # edits, joins, deletions...
            return
        if event.get("channel_type") == "im":
            log.info("slack_dm", channel=event.get("channel"), ts=event.get("ts"))
            await self.answer(client, event, thread_ts=event.get("thread_ts"))
            return
        log.info(
            "slack_message_seen",
            channel=event.get("channel"),
            channel_type=event.get("channel_type"),
            ts=event.get("ts"),
            thread_ts=event.get("thread_ts"),
        )


def build_app(settings: Settings, answerer: Answerer | None = None) -> AsyncApp:
    assert settings.slack_bot_token is not None
    app = AsyncApp(token=settings.slack_bot_token.get_secret_value())
    ops = SlackOps(answerer or OpsAgent(settings))
    app.event("app_mention")(ops.handle_mention)
    app.event("message")(ops.handle_message)
    return app


def _mark_ready(ready: bool) -> None:
    if ready:
        READY_FILE.touch()
    else:
        READY_FILE.unlink(missing_ok=True)


async def run(settings: Settings) -> None:
    await asyncio.to_thread(_mark_ready, False)
    if settings.slack_bot_token is None or settings.slack_app_token is None:
        # Stay up without Slack instead of crash-looping; the healthcheck stays red.
        log.error(
            "slack_disabled",
            reason="SLACK_BOT_TOKEN and SLACK_APP_TOKEN must both be set (see SL-0)",
        )
        await asyncio.Event().wait()
        return

    app = build_app(settings)
    identity = await app.client.auth_test()
    handler = AsyncSocketModeHandler(app, settings.slack_app_token.get_secret_value())
    await handler.connect_async()
    await asyncio.to_thread(_mark_ready, True)
    log.info(
        "slack_connected",
        bot_user=identity.get("user"),
        team=identity.get("team"),
        capture_channels=settings.slack_capture_channels,
    )
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.to_thread(_mark_ready, False)
        await handler.close_async()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    asyncio.run(run(settings))
