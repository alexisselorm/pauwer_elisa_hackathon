"""`silentctx-slackbot`: the Bolt app in Socket Mode (no public URL needed).

This process only connects to Slack and stays online; capture handlers, the context card
and the ops agent are registered here by SL-1 and SL-2. The bot reaches the context layer
through the API's MCP server, never by importing it (plan 002).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from silentctx.config import Settings, get_settings
from silentctx.logging import configure_logging, get_logger

# Written once connected; the compose healthcheck looks for it.
READY_FILE = Path("/tmp/silentctx-slackbot.ready")  # noqa: S108  # container-local marker
log = get_logger(__name__)


def build_app(settings: Settings) -> AsyncApp:
    assert settings.slack_bot_token is not None
    return AsyncApp(token=settings.slack_bot_token.get_secret_value())


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
