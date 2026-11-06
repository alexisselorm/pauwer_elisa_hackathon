"""`silentctx-slackbot`: the Bolt app in Socket Mode (no public URL needed).

For now it only says hello (debug: proves events reach us and replies get back), and logs
that channel messages arrive. Capture handlers, the context card and the ops agent replace
this in SL-1 and SL-2. The bot reaches the context layer through the API's MCP server,
never by importing it (plan 002).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_bolt.context.say.async_say import AsyncSay

from silentctx.config import Settings, get_settings
from silentctx.logging import configure_logging, get_logger

# Written once connected; the compose healthcheck looks for it.
READY_FILE = Path("/tmp/silentctx-slackbot.ready")  # noqa: S108  # container-local marker
log = get_logger(__name__)


def _is_from_a_bot(event: dict[str, Any]) -> bool:
    # Never answer bots (ourselves included): that is how reply loops start.
    return bool(event.get("bot_id")) or event.get("subtype") == "bot_message"


async def handle_mention(event: dict[str, Any], say: AsyncSay) -> None:
    """@silentctx in a channel: say hello in the thread of the mention."""
    if _is_from_a_bot(event) or event.get("subtype"):
        return
    log.info("slack_mention", channel=event.get("channel"), ts=event.get("ts"))
    await say(
        text=f"Hello <@{event.get('user')}>! :wave: silentctx is online.",
        thread_ts=event.get("thread_ts") or event.get("ts"),
    )


async def handle_message(event: dict[str, Any], say: AsyncSay) -> None:
    """A DM to the bot gets a hello. Channel messages are only logged for now (capture is
    SL-1); an @mention in a channel is answered by `handle_mention`, not here."""
    if _is_from_a_bot(event) or event.get("subtype"):  # edits, joins, deletions...
        return
    if event.get("channel_type") == "im":
        log.info("slack_dm", channel=event.get("channel"), ts=event.get("ts"))
        await say(text=f"Hello <@{event.get('user')}>! :wave: silentctx is online.")
        return
    log.info(
        "slack_message_seen",
        channel=event.get("channel"),
        channel_type=event.get("channel_type"),
        ts=event.get("ts"),
        thread_ts=event.get("thread_ts"),
    )


def build_app(settings: Settings) -> AsyncApp:
    assert settings.slack_bot_token is not None
    app = AsyncApp(token=settings.slack_bot_token.get_secret_value())
    app.event("app_mention")(handle_mention)
    app.event("message")(handle_message)
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
