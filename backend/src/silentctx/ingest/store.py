"""Capture state in Neo4j (plan 001 §5): `Message` events, `Thread` and `ChannelCursor`.

Events are immutable and idempotent on `event_id`, which already names the channel
(`slack:C123:1760422928.123456`), so a redelivered Slack event or a re-run backfill writes
nothing (ST-61, ST-62). Edits and deletes are new events that point at the original (R6).
`ingested_at` is stored for operations only and never read by a temporal query (R1).

`Thread` remembers what the capture loop has read and decided. A thread is due for triage
when it has a message newer than `last_triaged_ts` and is resolved or idle (BE-2.4).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, LiteralString

from neo4j import AsyncDriver
from pydantic import BaseModel

from silentctx.domain.models import Platform, UtcDatetime
from silentctx.graph.repository import py


class MessageIn(BaseModel):
    event_id: str
    platform: Platform
    channel: str  # Slack channel id, Teams channel id, ...
    thread_id: str
    event_time: UtcDatetime  # R1: the platform's own time
    platform_seq: str | None = None  # R7 tie-break: Slack ts, Teams created_iid
    author: str | None = None
    author_role: str | None = None
    text: str = ""
    edit_of: str | None = None  # an edit is a new event (R6)
    deletes: str | None = None  # so is a delete
    permalink: str | None = None
    resolution: bool = False  # a resolution signal (trigger.py)
    # Where the context card goes, for Slack threads.
    slack_channel: str | None = None
    slack_thread_ts: str | None = None


class MessageView(BaseModel):
    event_id: str
    event_time: UtcDatetime
    author: str | None = None
    author_role: str | None = None
    text: str
    platform: str | None = None


class ThreadView(BaseModel):
    thread_id: str
    channel: str
    platform: str | None = None
    status: str = "pending"
    forced: bool = False
    last_message_ts: UtcDatetime
    slack_channel: str | None = None
    slack_thread_ts: str | None = None
    card_ts: str | None = None
    assertion_ids: list[str] = []
    messages: list[MessageView] = []

    def text(self) -> str:
        return "\n".join(m.text for m in self.messages)


_STORE: LiteralString = """
UNWIND $rows AS r
OPTIONAL MATCH (old:Event {event_id: r.event_id})
WITH r WHERE old IS NULL
CREATE (m:Event:Message {event_id: r.event_id, event_time: r.event_time, ingested_at: $now,
  platform: r.platform, channel: r.channel, thread_id: r.thread_id,
  platform_seq: r.platform_seq, author: r.author, author_role: r.author_role, text: r.text,
  edit_of: r.edit_of, deletes: r.deletes, permalink: r.permalink})
MERGE (t:Thread {thread_id: r.thread_id})
  ON CREATE SET t.channel = r.channel, t.platform = r.platform, t.status = 'pending',
    t.slack_channel = r.slack_channel, t.slack_thread_ts = r.slack_thread_ts
CREATE (m)-[:IN_THREAD]->(t)
SET t.last_message_ts = CASE WHEN t.last_message_ts IS NULL OR r.event_time > t.last_message_ts
      THEN r.event_time ELSE t.last_message_ts END,
    t.resolved = coalesce(t.resolved, false) OR r.resolution
RETURN count(m) AS n
"""


class CaptureStore:
    def __init__(self, driver: AsyncDriver, database: str) -> None:
        self._driver = driver
        self._db = database

    async def _q(self, query: LiteralString, **params: Any) -> list[dict[str, Any]]:
        records, _, _ = await self._driver.execute_query(query, params, database_=self._db)
        return [py(dict(r)) for r in records]

    async def store(self, messages: Sequence[MessageIn], now: datetime) -> int:
        """Write new messages and update their threads. Returns how many were new."""
        unique = {m.event_id: m for m in messages}  # one batch may repeat an event
        if not unique:
            return 0
        rows = await self._q(_STORE, rows=[m.model_dump() for m in unique.values()], now=now)
        return int(rows[0]["n"]) if rows else 0

    # --- Channel watermark (ST-08) ------------------------------------------------------

    async def cursor(self, channel: str) -> str | None:
        rows = await self._q(
            "MATCH (c:ChannelCursor {channel: $c}) RETURN c.last_seen_ts AS ts", c=channel
        )
        return rows[0]["ts"] if rows else None

    async def advance_cursor(self, channel: str, ts: str) -> None:
        # Slack ts strings have a fixed width, so they compare correctly as text.
        await self._q(
            "MERGE (c:ChannelCursor {channel: $c}) "
            "SET c.last_seen_ts = CASE WHEN c.last_seen_ts IS NULL OR $ts > c.last_seen_ts "
            "THEN $ts ELSE c.last_seen_ts END",
            c=channel,
            ts=ts,
        )

    # --- Threads ---------------------------------------------------------------------------

    async def mark_resolved(
        self, message_event_id: str, at: datetime, *, forced: bool = False
    ) -> bool:
        """A ✅/🎉 reaction or the capture shortcut on a message: its thread is resolved and
        counts as active at `at`, so the sweeper triages it again."""
        rows = await self._q(
            "MATCH (:Message {event_id: $id})-[:IN_THREAD]->(t:Thread) "
            "SET t.resolved = true, t.forced = coalesce(t.forced, false) OR $forced, "
            "  t.last_message_ts = CASE WHEN $at > t.last_message_ts THEN $at "
            "    ELSE t.last_message_ts END "
            "RETURN t.thread_id AS id",
            id=message_event_id,
            at=at,
            forced=forced,
        )
        return bool(rows)

    async def due_threads(self, now: datetime, idle: timedelta, limit: int = 50) -> list[str]:
        """Threads with news since their last triage that are resolved or idle. A thread with
        no new message is never returned, so a sweep costs no LLM call (ST-09)."""
        rows = await self._q(
            "MATCH (t:Thread) WHERE t.last_message_ts > coalesce(t.last_triaged_ts, "
            "  datetime('0001-01-01T00:00:00Z')) "
            "AND (t.resolved OR t.last_message_ts <= $cutoff) "
            "RETURN t.thread_id AS id ORDER BY t.last_message_ts LIMIT $limit",
            cutoff=now - idle,
            limit=limit,
        )
        return [r["id"] for r in rows]

    async def thread(self, thread_id: str) -> ThreadView:
        """The thread as people see it now: each message at its latest edit, deletes removed."""
        rows = await self._q(
            "MATCH (t:Thread {thread_id: $id}) "
            "OPTIONAL MATCH (m:Message)-[:IN_THREAD]->(t) "
            "WITH t, m ORDER BY m.event_time, m.platform_seq, m.event_id "
            "RETURN t { .* } AS t, collect(m { .event_id, .event_time, .author, .author_role, "
            "  .text, .platform, .edit_of, .deletes }) AS ms",
            id=thread_id,
        )
        if not rows:
            raise LookupError(f"unknown thread {thread_id!r}")
        t, raw = rows[0]["t"], rows[0]["ms"]
        deleted = {m["deletes"] for m in raw if m.get("deletes")}
        latest = {m["edit_of"]: m for m in raw if m.get("edit_of")}  # ordered: last edit wins
        messages = []
        for m in raw:
            if m.get("edit_of") or m.get("deletes") or m["event_id"] in deleted:
                continue
            edit = latest.get(m["event_id"])
            # The edit's own event is the source: its text is what was quoted.
            shown = {**m, "event_id": edit["event_id"], "text": edit["text"]} if edit else m
            messages.append(MessageView.model_validate({**shown, "text": shown.get("text") or ""}))
        return ThreadView.model_validate(
            {**t, "forced": bool(t.get("forced")), "messages": messages}
        )

    async def set_triage(
        self,
        thread_id: str,
        *,
        status: str,
        kind: str,
        reason: str,
        seen_up_to: datetime,
        assertion_ids: Sequence[str] | None = None,
        capture_json: str | None = None,
    ) -> None:
        """The decision, remembered (BE-2.4). `seen_up_to` is the last message the decision
        read: anything newer re-triages the thread. `capture_json` is the pipeline's full
        result (steps, gates, decisions), kept for the console (UI-1)."""
        await self._q(
            "MATCH (t:Thread {thread_id: $id}) "
            "SET t.status = $status, t.triage_kind = $kind, t.triage_reason = $reason, "
            "  t.last_triaged_ts = $seen, "
            # A message (or ✅) that arrived while triage ran keeps the thread due.
            "  t.resolved = CASE WHEN t.last_message_ts > $seen THEN t.resolved ELSE false END, "
            "  t.forced = CASE WHEN t.last_message_ts > $seen THEN t.forced ELSE false END, "
            "  t.assertion_ids = CASE WHEN $aids IS NULL THEN t.assertion_ids ELSE $aids END, "
            "  t.capture_json = coalesce($capture, t.capture_json)",
            id=thread_id,
            status=status,
            kind=kind,
            reason=reason or "no reason given",
            seen=seen_up_to,
            aids=list(assertion_ids) if assertion_ids is not None else None,
            capture=capture_json,
        )

    async def set_card(self, thread_id: str, card_ts: str) -> None:
        await self._q(
            "MATCH (t:Thread {thread_id: $id}) SET t.card_ts = $ts", id=thread_id, ts=card_ts
        )

    async def replace_assertion(self, old_id: str, new_id: str) -> list[str]:
        """A correction from the card: the thread now shows the fixed version."""
        rows = await self._q(
            "MATCH (t:Thread) WHERE $old IN t.assertion_ids "
            "SET t.assertion_ids = [x IN t.assertion_ids | CASE WHEN x = $old THEN $new "
            "  ELSE x END] RETURN t.thread_id AS id",
            old=old_id,
            new=new_id,
        )
        return [r["id"] for r in rows]

    async def threads_with_assertions(self, assertion_ids: Sequence[str]) -> list[str]:
        rows = await self._q(
            "MATCH (t:Thread) WHERE any(x IN t.assertion_ids WHERE x IN $ids) "
            "RETURN t.thread_id AS id",
            ids=list(assertion_ids),
        )
        return [r["id"] for r in rows]

    async def record_card_action(
        self, *, event_id: str, at: datetime, author: str, action: str, thread_id: str | None
    ) -> None:
        """A click on the card is an event too (R1): corrections are recorded at it (R6ii)."""
        await self._q(
            "MERGE (e:Event:CardAction {event_id: $id}) "
            "ON CREATE SET e.event_time = $at, e.author = $author, e.action = $action, "
            "  e.thread_id = $thread, e.platform = 'slack'",
            id=event_id,
            at=at,
            author=author,
            action=action,
            thread=thread_id,
        )
