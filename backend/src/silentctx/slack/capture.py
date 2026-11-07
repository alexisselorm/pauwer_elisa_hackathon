"""Capture in Slack (SL-1, plan 002 §3.2): the handlers that feed ingestion, and the context
card.

Usability budget, enforced here: at most ONE bot message per thread (the card, edited in
place with `chat.update`, never re-posted) and zero required actions. The buttons are
optional: with no click the assertion stays `unverified` and is still used (ST-07). The one
clarifying question (V3 candidate, or a date it couldn't read) is part of the card, not a
second message. Ignore decisions and captures from outside Slack go to #context-log, never
to a work channel.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from silentctx.context.models import AssertionIn, AssertionView, BindingIn, Scope
from silentctx.domain.models import AssertionStatus
from silentctx.errors import WriteRejectedError
from silentctx.extraction.graph import CaptureResult
from silentctx.graph.repository import ContextRepository
from silentctx.ingest.capture import CaptureLoop
from silentctx.ingest.slack_events import SlackIngest
from silentctx.ingest.store import CaptureStore, MessageIn, ThreadView
from silentctx.logging import get_logger
from silentctx.slack.actions import find_channel
from silentctx.slack.format import actions, button, context

log = get_logger(__name__)

CARD_CONFIRM = "card_confirm"
CARD_FIX = "card_fix"
CARD_RETRACT = "card_retract"
CARD_BIND = "card_bind"
CARD_FIX_VIEW = "card_fix_modal"
CAPTURE_SHORTCUT = "capture_message"  # slack/manifest.yaml
LOG_CHANNEL_NAME = "context-log"

_AVOID_WORDS = {
    "replace_sfp": "replacing the SFP again",
    "reboot_node": "rebooting the unit",
    "power_cycle_radio": "power-cycling the radio",
}
_STATUS = {
    AssertionStatus.UNVERIFIED: "unverified, in use",
    AssertionStatus.BELIEVED_TRUE: ":white_check_mark: confirmed",
    AssertionStatus.DISPUTED: ":warning: disputed",
    AssertionStatus.SUPERSEDED: "superseded",
    AssertionStatus.EXPIRED: "expired",
}
_SCOPE_FIELDS = (Scope.OBSERVED_ON, Scope.APPLIES_TO, Scope.ACTIVITY)


def _now() -> datetime:
    return datetime.now(UTC)


def _who(author: str | None) -> str:
    if author and author[:1] in "UW" and author[1:].isalnum() and author.isupper():
        return f"<@{author}>"
    return (author or "unknown").split(" ")[0]


def _label(entity_id: str) -> str:
    name = entity_id.removeprefix("TOPIC:").replace("_", " ")
    return name.replace("sfp", "SFP")


def _scope_line(view: AssertionView) -> str:
    by = {s: [b.entity_id for b in view.bindings if b.scope is s] for s in _SCOPE_FIELDS}
    models = [e for e in by[Scope.APPLIES_TO] if not e.startswith("Gen")]
    gens = [e for e in by[Scope.APPLIES_TO] if e.startswith("Gen")]
    parts = []
    if models or gens:
        head = ", ".join(models) or ", ".join(gens)
        parts.append(f"{head} ({', '.join(gens)})" if models and gens else head)
    if by[Scope.OBSERVED_ON]:
        parts.append("seen on " + ", ".join(by[Scope.OBSERVED_ON]))
    if by[Scope.ACTIVITY]:
        parts.append("after " + ", ".join(_label(e) for e in by[Scope.ACTIVITY]))
    return " · ".join(parts) or "no scope"


def _value(aid: str, thread_id: str, **extra: str) -> dict[str, str]:
    return {"a": aid, "t": thread_id, **extra}


def card_blocks(
    views: list[AssertionView],
    thread: ThreadView,
    extra_sources: dict[str, Counter[str]],
) -> tuple[str, list[dict[str, Any]]]:
    """The context card: one section per assertion the thread produced."""
    blocks: list[dict[str, Any]] = []
    texts = []
    for v in views:
        if v.retracted_at is not None:
            blocks.append(context(f":no_entry_sign: Not knowledge: removed. _{v.statement}_"))
            continue
        q = v.qualifiers
        header = f":pushpin: *Captured for {_scope_line(v)}*"
        body = [header, v.statement]
        if q.get("avoid"):
            body.append("*Avoid:* " + ", ".join(_AVOID_WORDS.get(a, _label(a)) for a in q["avoid"]))
        texts.append(f"Captured: {v.statement}")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(body)}})

        # Who said it: the latest message it derives from, never a click on this card.
        own = sorted(
            (
                c
                for c in v.citations
                if c.recorded_at is not None and not (c.source_ref or "").startswith("card:")
            ),
            key=lambda c: c.recorded_at or v.recorded_at,
        )
        meta = [
            f"Valid from <!date^{int(v.valid_from.timestamp())}^{{date_short_pretty}} {{time}}"
            f"|{v.valid_from:%Y-%m-%d %H:%M} UTC>"
        ]
        if own:
            meta.append(f"source: {_who(own[-1].author)} (this thread)")
        tele = q.get("telemetry")
        if tele == "agrees":
            meta.append("telemetry agrees ✓")
        elif tele == "disagrees":
            meta.append(":warning: telemetry disagrees")
        for kind, n in sorted(extra_sources.get(v.assertion_id, Counter()).items()):
            meta.append(f"+{n} source ({kind})")
        # A saved ✏️ Fix is a source of the fixed version: say who fixed it and when.
        fixes = [c for c in v.citations if ":fix:" in (c.source_ref or "") and c.recorded_at]
        if fixes:
            last = max(fixes, key=lambda c: c.recorded_at or v.recorded_at)
            at = last.recorded_at or v.recorded_at
            meta.append(
                f":pencil2: fixed by {_who(last.author)} "
                f"<!date^{int(at.timestamp())}^{{time}}|{at:%H:%M} UTC>"
            )
        meta.append(_STATUS.get(v.status, str(v.status)))
        blocks.append(context(" · ".join(meta)))

        candidates = [b.entity_id for b in v.bindings if b.candidate]
        if candidates:
            question = q.get("question") or "Which one is it?"
            blocks.append(context(f":grey_question: {question}"))
            blocks.append(
                actions(
                    f"bind:{v.assertion_id}",
                    *[
                        button(e, CARD_BIND, _value(v.assertion_id, thread.thread_id, e=e))
                        for e in candidates[:5]
                    ],
                )
            )
        if q.get("time_note"):
            blocks.append(context(f":grey_question: {q['time_note']}. Use ✏️ Fix to change it."))
        buttons = []
        if v.status is AssertionStatus.UNVERIFIED:
            buttons.append(
                button("✅ Correct", CARD_CONFIRM, _value(v.assertion_id, thread.thread_id))
            )
        buttons += [
            button("✏️ Fix", CARD_FIX, _value(v.assertion_id, thread.thread_id)),
            button("🚫 Not knowledge", CARD_RETRACT, _value(v.assertion_id, thread.thread_id)),
        ]
        blocks.append(actions(f"card:{v.assertion_id}", *buttons))
    return "\n".join(texts) or "Context card", blocks


def fix_modal(view: AssertionView, thread_id: str) -> dict[str, Any]:
    def field(block_id: str, label: str, initial: str, multiline: bool = False) -> dict[str, Any]:
        return {
            "type": "input",
            "block_id": block_id,
            "optional": block_id != "statement",
            "label": {"type": "plain_text", "text": label},
            "element": {
                "type": "plain_text_input",
                "action_id": "v",
                "multiline": multiline,
                "initial_value": initial,
            },
        }

    by = {s: ", ".join(b.entity_id for b in view.bindings if b.scope is s) for s in _SCOPE_FIELDS}
    until = f"{view.valid_to:%Y-%m-%d %H:%M} UTC" if view.valid_to else "still true"
    return {
        "type": "modal",
        "callback_id": CARD_FIX_VIEW,
        "private_metadata": json.dumps({"a": view.assertion_id, "t": thread_id}),
        "title": {"type": "plain_text", "text": "Fix this knowledge"},
        "submit": {"type": "plain_text", "text": "Save fix"},
        "blocks": [
            field("statement", "What is true", view.statement, multiline=True),
            field("observed_on", "Seen on (entity ids)", by[Scope.OBSERVED_ON]),
            field("applies_to", "Applies to (models, generations)", by[Scope.APPLIES_TO]),
            field("activity", "During (activity)", by[Scope.ACTIVITY]),
            context(
                f"Valid {view.valid_from:%Y-%m-%d %H:%M} UTC → {until}. A fix corrects what was "
                "captured; it keeps this validity (it was always so)."
            ),
        ],
    }


def _ids(text: str | None) -> list[str]:
    return [p.strip() for p in (text or "").replace(";", ",").split(",") if p.strip()]


class CaptureCards:
    """Posts and updates the context card, and handles its buttons."""

    def __init__(
        self,
        store: CaptureStore,
        repo: ContextRepository,
        *,
        log_channel: str | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store, self.repo = store, repo
        self.log_channel = log_channel
        self.clock = clock
        self.client: AsyncWebClient | None = None
        self._log_target: str | None = None
        self._log_looked = False
        self._lock = asyncio.Lock()

    async def log_target(self, client: AsyncWebClient) -> str | None:
        """#context-log: SLACK_CONTEXT_LOG_CHANNEL, else a channel named #context-log."""
        if not self._log_looked:
            self._log_looked = True
            self._log_target = self.log_channel
            if self._log_target is None:
                try:
                    self._log_target = await find_channel(client, LOG_CHANNEL_NAME)
                except Exception:
                    log.warning("context_log_lookup_failed", exc_info=True)
        return self._log_target

    async def _note(self, text: str) -> None:
        log.info("context_log", text=text)
        if self.client is not None and (target := await self.log_target(self.client)):
            await self.client.chat_postMessage(channel=target, text=text)

    async def announce(self, thread: ThreadView, result: CaptureResult) -> None:
        on_slack = thread.slack_channel and thread.slack_thread_ts
        if result.status == "captured" and on_slack:
            await self.refresh(thread.thread_id)
        elif result.status == "captured":
            for item in result.items:
                if item.assertion_id and item.new:
                    await self._note(
                        f":inbox_tray: Captured from {thread.platform} ({thread.thread_id}): "
                        f"{item.draft.statement}"
                    )
        else:
            await self._note(
                f":see_no_evil: {result.status} {thread.thread_id} ({result.kind}): {result.reason}"
            )
        corroborated = [a for i in result.items if i.decision for a in i.decision.corroborates]
        if corroborated:
            for other in await self.store.threads_with_assertions(corroborated):
                if other != thread.thread_id:
                    await self.refresh(other)

    async def refresh(self, thread_id: str) -> None:
        """Post the card once, or edit it in place."""
        if self.client is None:
            return
        async with self._lock:
            thread = await self.store.thread(thread_id)
            if not (thread.slack_channel and thread.slack_thread_ts and thread.assertion_ids):
                return
            views = await self.repo.assertions(thread.assertion_ids)
            extra: dict[str, Counter[str]] = {}
            for v in views:
                others = await self.repo.assertions(v.corroborates)
                extra[v.assertion_id] = Counter(
                    (o.citations[0].source_type or "other") for o in others if o.citations
                )
            text, blocks = card_blocks(views, thread, extra)
            if thread.card_ts:
                await self.client.chat_update(
                    channel=thread.slack_channel, ts=thread.card_ts, text=text, blocks=blocks
                )
            else:
                posted = await self.client.chat_postMessage(
                    channel=thread.slack_channel,
                    thread_ts=thread.slack_thread_ts,
                    text=text,
                    blocks=blocks,
                )
                await self.store.set_card(thread_id, posted["ts"])

    # --- Buttons ---------------------------------------------------------------------------

    async def _click(self, body: dict[str, Any], action: str) -> tuple[dict[str, str], datetime]:
        v: dict[str, str] = json.loads(body["actions"][0]["value"])
        at = self.clock()
        click = body["actions"][0].get("action_ts", at.timestamp())
        await self.store.record_card_action(
            event_id=f"card:{v['a']}:{action}:{click}",
            at=at,
            author=body["user"]["id"],
            action=action,
            thread_id=v["t"],
        )
        return v, at

    async def on_confirm(self, ack: Any, body: dict[str, Any]) -> None:
        await ack()
        v, _ = await self._click(body, "confirm")
        await self.repo.confirm(v["a"])
        await self.refresh(v["t"])

    async def on_retract(self, ack: Any, body: dict[str, Any]) -> None:
        await ack()
        v, at = await self._click(body, "retract")
        await self.repo.retract(v["a"], at)
        await self.refresh(v["t"])

    async def on_bind(self, ack: Any, body: dict[str, Any]) -> None:
        await ack()
        v, _ = await self._click(body, "bind")
        try:
            await self.repo.bind_candidate(v["a"], v["e"])
        except WriteRejectedError:
            log.info("bind_ignored", assertion_id=v["a"], entity_id=v["e"])  # already answered
        await self.refresh(v["t"])

    async def on_fix(self, ack: Any, body: dict[str, Any], client: AsyncWebClient) -> None:
        await ack()
        v: dict[str, str] = json.loads(body["actions"][0]["value"])
        found = await self.repo.assertions([v["a"]])
        if found:
            await client.views_open(trigger_id=body["trigger_id"], view=fix_modal(found[0], v["t"]))

    async def on_fix_submit(self, ack: Any, body: dict[str, Any], view: dict[str, Any]) -> None:
        """Saving the modal is a correction (R6 ii): the old version is retracted at the
        click, the fix keeps the original validity, and there is no SUPERSEDES (ST-05)."""
        meta = json.loads(view["private_metadata"])
        values = {k: v["v"].get("value") for k, v in view["state"]["values"].items()}
        bindings = [
            BindingIn(entity_id=e, scope=s, surface_form="fixed in Slack")
            for s in _SCOPE_FIELDS
            for e in _ids(values.get(s.value))
        ]
        known = await self.repo.entity_types([b.entity_id for b in bindings])
        unknown = [b.entity_id for b in bindings if b.entity_id not in known]
        if unknown or not bindings:
            block = next(
                (s.value for s in _SCOPE_FIELDS if set(_ids(values.get(s.value))) & set(unknown)),
                "observed_on",
            )
            msg = f"Unknown: {', '.join(unknown)}" if unknown else "Name at least one entity"
            await ack(response_action="errors", errors={block: msg})
            return
        await ack()
        old = (await self.repo.assertions([meta["a"]]))[0]
        at = self.clock()
        event_id = f"card:{meta['a']}:fix:{view.get('id', at.timestamp())}"
        await self.store.record_card_action(
            event_id=event_id, at=at, author=body["user"]["id"], action="fix", thread_id=meta["t"]
        )
        fixed = AssertionIn(
            statement=values.get("statement") or old.statement,
            topic=old.topic,
            qualifiers=old.qualifiers,
            status=AssertionStatus.BELIEVED_TRUE,  # a person wrote it
            bindings=bindings,
            sources=await self.repo.sources(meta["a"]),
        )
        try:
            out = await self.repo.correct(meta["a"], fixed, event_id)
        except WriteRejectedError:
            log.exception("fix_rejected", assertion_id=meta["a"])
            return
        await self.store.replace_assertion(meta["a"], out.assertion_id)
        await self.refresh(meta["t"])


class CaptureHandlers:
    """Message, reaction and shortcut events -> ingestion (BE-2.1) and the loop (BE-2.4).

    A reply the word list doesn't recognise as a resolution ("counters moving again, kiitos")
    is asked to the FAST_MODEL classifier in the background; a yes resolves the thread. A
    failed check changes nothing: the idle trigger still reads the thread."""

    def __init__(
        self,
        ingest: SlackIngest,
        loop: CaptureLoop,
        resolves: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self.ingest, self.loop, self.resolves = ingest, loop, resolves
        self._checks: set[asyncio.Task[None]] = set()

    async def on_message(self, event: dict[str, Any]) -> None:
        msg = await self.ingest.on_message(event)
        if msg is None:
            return
        if msg.resolution:
            self.loop.kick()
        elif (
            self.resolves is not None
            and msg.text
            and event.get("thread_ts")
            not in (
                None,
                event.get("ts"),
            )
        ):
            task = asyncio.create_task(self._classify(msg, msg.text))
            self._checks.add(task)
            task.add_done_callback(self._checks.discard)

    async def _classify(self, msg: MessageIn, text: str) -> None:
        assert self.resolves is not None
        try:
            thread = await self.ingest.store.thread(msg.thread_id)
            start = thread.messages[0].text if thread.messages else ""
            if await self.resolves(text, start) and await self.ingest.store.mark_resolved(
                msg.event_id, msg.event_time
            ):
                log.info("resolution_by_model", event_id=msg.event_id)
                self.loop.kick()
        except Exception:
            log.warning("resolution_check_failed", event_id=msg.event_id, exc_info=True)

    async def on_reaction(self, event: dict[str, Any]) -> None:
        if await self.ingest.on_reaction(event):
            self.loop.kick()

    async def on_shortcut(self, ack: Any, body: dict[str, Any], client: AsyncWebClient) -> None:
        await ack()
        channel = body["channel"]["id"]
        if await self.ingest.capture_shortcut(channel, body["message"]):
            self.loop.kick()
            note = ":pushpin: Got it. I'll read this thread and post a context card."
        else:
            note = "I can't capture that message (bot message, DM or an unwatched channel)."
        await client.chat_postEphemeral(channel=channel, user=body["user"]["id"], text=note)
