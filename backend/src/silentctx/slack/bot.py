"""`silentctx-slackbot`: the Bolt app in Socket Mode (no public URL needed).

Answers questions with the ops agent (@mention in a channel, or a DM), posts simulator faults
with the agent's advice and outcome buttons (SL-2, `slack/actions.py`), and captures what
people say in every channel it is in (SL-1, `slack/capture.py`): messages become events, the
capture loop triages and extracts, and one context card is posted per thread. The agent
reaches the context layer through the API's MCP server only, never by importing it
(plan 002); capture is the context layer's own ingestion, so it writes to Neo4j directly.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from silentctx.agents.ops_agent import OpsAgent, OpsAnswer
from silentctx.config import Settings, get_settings
from silentctx.extraction.graph import Extractor
from silentctx.extraction.llm import LlmJudge
from silentctx.graph.driver import create_driver
from silentctx.graph.repository import ContextRepository
from silentctx.ingest.capture import CaptureLoop
from silentctx.ingest.slack_events import SlackIngest
from silentctx.ingest.store import CaptureStore
from silentctx.llm.embeddings import FeatherlessEmbedder, embed_query_or_none
from silentctx.logging import configure_logging, get_logger
from silentctx.slack import actions as ops_actions
from silentctx.slack import capture as cap
from silentctx.slack.format import format_answer, text_blocks
from silentctx.telemetry.store import TelemetryStore

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

    def __init__(self, answerer: Answerer, capture: cap.CaptureHandlers | None = None) -> None:
        self.answerer = answerer
        self.capture = capture

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
            blocks = ops_actions.answer_blocks(result, question=question)
        except Exception as exc:  # the user always gets a reply
            log.exception("ops_agent_failed", channel=channel, ts=event.get("ts"))
            text = (
                ":warning: Sorry, I couldn't answer that right now "
                f"({type(exc).__name__}). Please try again in a minute."
            )
            blocks = text_blocks(text)
        await client.chat_update(channel=channel, ts=placeholder["ts"], text=text, blocks=blocks)

    async def react_seen(self, client: AsyncWebClient, event: dict[str, Any]) -> None:
        """👀 on the mention: seen, an answer is coming. Never blocks the answer."""
        try:
            await client.reactions_add(channel=event["channel"], timestamp=event["ts"], name="eyes")
        except Exception as exc:  # noqa: BLE001  # e.g. missing reactions:write, already reacted
            log.warning("slack_reaction_failed", error=f"{type(exc).__name__}: {exc}")

    async def handle_mention(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        """@silentctx in a channel: answer in the thread of the mention."""
        if _is_from_a_bot(event) or event.get("subtype"):
            return
        log.info("slack_mention", channel=event.get("channel"), ts=event.get("ts"))
        await self.react_seen(client, event)
        await self.answer(client, event, thread_ts=event.get("thread_ts") or event.get("ts"))

    async def handle_message(self, event: dict[str, Any], client: AsyncWebClient) -> None:
        """A DM to the bot is a question. Every channel message (edits and deletes too) is
        captured; an @mention in a channel is also answered by `handle_mention`."""
        if event.get("channel_type") == "im":
            if _is_from_a_bot(event) or event.get("subtype"):  # edits, bots...
                return
            log.info("slack_dm", channel=event.get("channel"), ts=event.get("ts"))
            await self.answer(client, event, thread_ts=event.get("thread_ts"))
            return
        if self.capture is not None:
            await self.capture.on_message(event)


def build_actions(settings: Settings, agent: OpsAgent) -> ops_actions.OpsActions:
    return ops_actions.OpsActions(
        agent,
        agent,
        network=ops_actions.SimNetwork(settings.sim_url) if settings.sim_url else None,
        alarm_channel=settings.slack_alarm_channel,
        gap_channel=settings.slack_gap_channel,
        capture_channels=settings.slack_capture_channels,
        settle=timedelta(seconds=settings.bounce_settle_s),
    )


def build_app(
    settings: Settings,
    answerer: Answerer,
    actions: ops_actions.OpsActions,
    capture: cap.CaptureHandlers | None = None,
    cards: cap.CaptureCards | None = None,
) -> AsyncApp:
    assert settings.slack_bot_token is not None
    app = AsyncApp(token=settings.slack_bot_token.get_secret_value())
    ops = SlackOps(answerer, capture)
    app.event("app_mention")(ops.handle_mention)
    app.event("message")(ops.handle_message)
    if capture is not None:
        app.event("reaction_added")(capture.on_reaction)
        app.shortcut(cap.CAPTURE_SHORTCUT)(capture.on_shortcut)
    if cards is not None:
        app.action(cap.CARD_CONFIRM)(cards.on_confirm)
        app.action(cap.CARD_RETRACT)(cards.on_retract)
        app.action(cap.CARD_BIND)(cards.on_bind)
        app.action(cap.CARD_FIX)(cards.on_fix)
        app.view(cap.CARD_FIX_VIEW)(cards.on_fix_submit)
    for action_id in (ops_actions.RUN_BOUNCE, ops_actions.DONE_MANUALLY, ops_actions.DIDNT_WORK):
        app.action(action_id)(actions.on_outcome)
    for action_id in (ops_actions.GAP_ASK, ops_actions.GAP_DONT_ASK):
        app.action(action_id)(actions.on_gap)
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

    agent = OpsAgent(settings)
    actions = build_actions(settings, agent)
    driver = create_driver(settings)
    repo = ContextRepository(driver, settings.neo4j_database)
    store = CaptureStore(driver, settings.neo4j_database)
    embedder = FeatherlessEmbedder.from_settings(settings)

    async def embed(text: str) -> list[float] | None:
        return await embed_query_or_none(embedder, text)

    judge = LlmJudge(settings)
    extractor = Extractor(repo, judge, TelemetryStore(settings.data_dir, settings.sim_url), embed)
    cards = cap.CaptureCards(store, repo, log_channel=settings.slack_context_log_channel)
    loop = CaptureLoop(
        store,
        extractor,
        cards.announce,
        idle=timedelta(minutes=settings.capture_idle_minutes),
        every_s=settings.capture_sweep_seconds,
    )
    ingest = SlackIngest(store, channels=settings.slack_capture_channels)
    capture = cap.CaptureHandlers(ingest, loop, judge.resolves)
    app = build_app(settings, agent, actions, capture, cards)
    identity = await app.client.auth_test()
    cards.client = app.client
    ingest.workspace_url = (identity.get("url") or "").rstrip("/") or None
    ingest.persona_bot_id = identity.get("bot_id")
    if (log_target := await cards.log_target(app.client)) is not None:
        ingest.skip.add(log_target)  # the audit channel is not a source
    handler = AsyncSocketModeHandler(app, settings.slack_app_token.get_secret_value())
    await handler.connect_async()
    await ingest.catch_up(app.client)  # Socket Mode does not replay what was missed (ST-08)
    sweeper = asyncio.create_task(loop.run(), name="capture-loop")
    watcher = None
    if actions.network is not None:
        watcher = asyncio.create_task(actions.watch(app.client), name="fault-watch")
    else:
        log.warning("fault_watch_off", reason="SIM_URL is not set: no network feed to watch")
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
        sweeper.cancel()
        if watcher is not None:
            watcher.cancel()
        await driver.close()
        await asyncio.to_thread(_mark_ready, False)
        await handler.close_async()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    asyncio.run(run(settings))
