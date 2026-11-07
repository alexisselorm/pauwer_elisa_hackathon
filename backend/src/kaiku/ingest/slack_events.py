"""Slack messages -> `Message` events (plan 001 WS2, BE-2.1).

`event_time` is the Slack `ts` (R1). An edit (`message_changed`) and a delete
(`message_deleted`) are new events at their own time that point at the original (R6).
Everything is idempotent on the event id, so a Slack retry and the catch-up overlap are
harmless (ST-61, ST-08).

Socket Mode does not replay events missed while the bot was down, so on startup
`catch_up` reads `conversations.history` / `conversations.replies` from each channel's
watermark. Every channel the bot is in is watched; `SLACK_CAPTURE_CHANNELS` narrows that
on purpose when set.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from kaiku.domain.models import Platform
from kaiku.domain.time import datetime_to_slack_ts, slack_ts_to_datetime
from kaiku.ingest import trigger
from kaiku.ingest.store import CaptureStore, MessageIn
from kaiku.ingest.threads import slack_event_id, slack_thread_id
from kaiku.logging import get_logger

log = get_logger(__name__)

# A reply to an old thread is not in conversations.history after the watermark, so parents
# this far back are re-read for new replies.
CATCHUP_LOOKBACK = timedelta(days=7)
# Subtypes that are people's words. Everything else (joins, topic changes, bots) is not.
_WORDS = {None, "thread_broadcast", "file_share", "me_message"}


def _now() -> datetime:
    return datetime.now(UTC)


def _from_bot(m: Mapping[str, Any]) -> bool:
    return bool(m.get("bot_id")) or m.get("subtype") == "bot_message"


class SlackIngest:
    def __init__(
        self,
        store: CaptureStore,
        *,
        channels: Collection[str] = (),
        skip_channels: Collection[str] = (),
        workspace_url: str | None = None,
        persona_bot_id: str | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store = store
        # The demo replay (002 §3.6) posts people's words as personas through this bot:
        # a message from this bot WITH a `username` override is a person's; the bot's own
        # cards and answers never set one, and other bots are never captured.
        self.persona_bot_id = persona_bot_id
        self.channels = set(channels)
        self.skip = set(skip_channels)
        self.workspace_url = (workspace_url or "").rstrip("/") or None
        self.clock = clock

    def _not_people(self, m: Mapping[str, Any]) -> bool:
        persona = bool(m.get("username")) and m.get("bot_id") == self.persona_bot_id
        return _from_bot(m) and not (self.persona_bot_id and persona)

    def watched(self, channel: str | None) -> bool:
        if not channel or channel in self.skip:
            return False
        return not self.channels or channel in self.channels

    def _permalink(self, channel: str, ts: str, thread_ts: str | None) -> str | None:
        if self.workspace_url is None:
            return None
        link = f"{self.workspace_url}/archives/{channel}/p{ts.replace('.', '')}"
        if thread_ts and thread_ts != ts:
            link += f"?thread_ts={thread_ts}&cid={channel}"
        return link

    def _message(
        self, channel: str, m: Mapping[str, Any], *, event_id: str, at: str, **extra: Any
    ) -> MessageIn:
        thread_ts = m.get("thread_ts") or m["ts"]
        text = m.get("text") or ""
        return MessageIn(
            event_id=event_id,
            platform=Platform.SLACK,
            channel=channel,
            thread_id=slack_thread_id(channel, m),
            event_time=slack_ts_to_datetime(at),
            platform_seq=at,
            author=m.get("user") or m.get("username"),
            text=text,
            permalink=self._permalink(channel, m["ts"], m.get("thread_ts")),
            resolution=trigger.is_resolution(text),
            slack_channel=channel,
            slack_thread_ts=thread_ts,
            **extra,
        )

    def to_message(self, event: Mapping[str, Any]) -> MessageIn | None:
        """A Slack `message` event (live or from history) as an event, or None if it is not
        a person's words in a watched channel."""
        channel = event.get("channel")
        if not self.watched(channel) or event.get("channel_type") == "im":
            return None
        assert channel is not None
        subtype = event.get("subtype")
        if subtype == "message_changed":
            m = event.get("message") or {}
            if self._not_people(m) or "ts" not in m:
                return None
            at = (m.get("edited") or {}).get("ts") or event["ts"]
            original = slack_event_id(channel, m["ts"])
            return self._message(
                channel, m, event_id=f"{original}:edit:{at}", at=at, edit_of=original
            )
        if subtype == "message_deleted":
            prev = event.get("previous_message") or {"ts": event.get("deleted_ts")}
            if self._not_people(prev) or not prev.get("ts"):
                return None
            original = slack_event_id(channel, prev["ts"])
            msg = self._message(
                channel,
                prev,
                event_id=f"{original}:delete:{event['ts']}",
                at=event["ts"],
                deletes=original,
            )
            return msg.model_copy(update={"text": "", "resolution": False})
        if subtype not in _WORDS | {"bot_message"} or self._not_people(event) or "ts" not in event:
            return None
        return self._message(
            channel, event, event_id=slack_event_id(channel, event["ts"]), at=event["ts"]
        )

    async def _store(self, messages: list[MessageIn]) -> int:
        new = await self.store.store(messages, self.clock())
        by_channel: dict[str, str] = {}
        for m in messages:
            seq = m.platform_seq or ""
            by_channel[m.channel] = max(by_channel.get(m.channel, ""), seq)
        for channel, ts in by_channel.items():
            await self.store.advance_cursor(channel, ts)
        return new

    async def on_message(self, event: Mapping[str, Any]) -> MessageIn | None:
        """Live event. Returns the message if it was new (a retry returns None)."""
        msg = self.to_message(event)
        if msg is None:
            return None
        return msg if await self._store([msg]) else None

    async def on_reaction(self, event: Mapping[str, Any]) -> bool:
        """✅/🎉 on a message resolves its thread (002 §3.2)."""
        item = event.get("item") or {}
        channel, ts = item.get("channel"), item.get("ts")
        if not trigger.is_resolution_reaction(event.get("reaction") or "") or not ts:
            return False
        if not self.watched(channel):
            return False
        assert channel is not None
        at = slack_ts_to_datetime(event.get("event_ts") or ts)
        return await self.store.mark_resolved(slack_event_id(channel, ts), at)

    async def capture_shortcut(self, channel: str, message: Mapping[str, Any]) -> bool:
        """ "Capture this as knowledge": store the message if it was missed, then force the
        thread through triage."""
        msg = self.to_message({**message, "channel": channel})
        if msg is None:
            return False
        await self._store([msg])
        return await self.store.mark_resolved(msg.event_id, self.clock(), forced=True)

    # --- Catch-up sweep (ST-08) -----------------------------------------------------------

    async def watched_channels(self, client: AsyncWebClient) -> list[str]:
        if self.channels:
            return sorted(self.channels - self.skip)
        try:
            found = await self._member_of(client, "public_channel,private_channel")
        except SlackApiError as exc:  # installed before groups:read was in the manifest
            if exc.response.get("error") != "missing_scope":
                raise
            log.warning("catch_up_public_only", reason="reinstall the app to add groups:read")
            found = await self._member_of(client, "public_channel")
        return [c for c in found if self.watched(c)]

    @staticmethod
    async def _member_of(client: AsyncWebClient, types: str) -> list[str]:
        found: list[str] = []
        cursor = None
        while True:
            page = await client.conversations_list(
                types=types, exclude_archived=True, limit=200, cursor=cursor
            )
            channels: list[dict[str, Any]] = page.get("channels", [])
            found += [c["id"] for c in channels if c.get("is_member")]
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return found

    async def catch_up(self, client: AsyncWebClient) -> int:
        """Ingest what was posted while the bot was down. Returns the number of new events."""
        total = 0
        for channel in await self.watched_channels(client):
            try:
                total += await self._catch_up_channel(client, channel)
            except Exception:  # one channel must not stop the others
                log.exception("catch_up_failed", channel=channel)
        log.info("catch_up_done", new_events=total)
        return total

    async def _catch_up_channel(self, client: AsyncWebClient, channel: str) -> int:
        mark = await self.store.cursor(channel)
        oldest = "0"
        if mark:
            oldest = datetime_to_slack_ts(slack_ts_to_datetime(mark) - CATCHUP_LOOKBACK)
        messages: list[MessageIn] = []
        cursor = None
        while True:
            page = await client.conversations_history(
                channel=channel, oldest=oldest, limit=200, cursor=cursor
            )
            history: list[dict[str, Any]] = page.get("messages", [])
            for m in history:
                if (msg := self.to_message({**m, "channel": channel})) is not None:
                    messages.append(msg)
                if m.get("reply_count") and (mark is None or m.get("latest_reply", "") > mark):
                    messages += await self._replies(client, channel, m["ts"])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return await self._store(messages) if messages else 0

    async def _replies(self, client: AsyncWebClient, channel: str, ts: str) -> list[MessageIn]:
        out: list[MessageIn] = []
        cursor = None
        while True:
            page = await client.conversations_replies(
                channel=channel, ts=ts, limit=200, cursor=cursor
            )
            replies: list[dict[str, Any]] = page.get("messages", [])
            for m in replies:
                if m.get("ts") != ts and (msg := self.to_message({**m, "channel": channel})):
                    out.append(msg)
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return out
