"""BE-2.1 / BE-2.4 / BE-3.1-3.3 / SL-1 without Neo4j or an LLM: backfill times, trigger,
gates V1/V2, time resolution, linking + V3, reconcile decisions, Slack event mapping and
the context card."""

from __future__ import annotations

import itertools
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from silentctx.context.models import AssertionView, Binding, Citation, Scope
from silentctx.domain.models import AssertionStatus
from silentctx.extraction import gates, reconcile
from silentctx.extraction.link import link, ports
from silentctx.extraction.models import Mention, Relation, StatedTimeOut, TriageKind
from silentctx.ingest import trigger
from silentctx.ingest.backfill import elisa_example, teams_message
from silentctx.ingest.slack_events import SlackIngest
from silentctx.ingest.store import MessageIn, MessageView, ThreadView
from silentctx.seed.sources import MockData
from silentctx.slack import capture as cap
from tests.capture_helpers import (
    ELISA_DIR,
    ELISA_RULE,
    MOCK_DATA,
    elisa_thread,
    inventory,
    q,
    thread_of,
)

MSG8 = datetime(2025, 10, 14, 6, 22, 8, tzinfo=UTC)

# --- BE-2.1: backfill --------------------------------------------------------------------


def test_elisa_example_is_dated_in_helsinki_and_strictly_increasing() -> None:
    chat = [m for m in elisa_example(ELISA_DIR) if m.thread_id == "elisa:teams_chat"]
    assert len(chat) == 12
    assert chat[7].event_time == MSG8  # ST-21
    times = [m.event_time for m in chat]
    assert all(a < b for a, b in itertools.pairwise(times))
    email = next(m for m in elisa_example(ELISA_DIR) if m.platform == "email")
    assert "disabling and enabling the interface" in email.text
    assert not email.text.startswith("From:")


def test_teams_message_keeps_its_original_time() -> None:
    m = next(MockData(MOCK_DATA).teams_messages())
    msg = teams_message(m)
    assert msg.event_time.isoformat().replace("+00:00", "Z") == m["created_at"]
    assert msg.thread_id == f"teams:{m['conversation_id']}"


# --- BE-2.4: trigger ---------------------------------------------------------------------


def test_trigger_fires_on_the_case_not_on_chatter() -> None:
    """ST-03: no resolution signal in corpus chatter or held-out H-04; message 9 fires."""
    chatter = [
        m["text"]
        for m in MockData(MOCK_DATA).teams_messages()
        if m["text"].startswith("generic ops chatter")
    ]
    heldout = [
        json.loads(line)
        for line in (MOCK_DATA.parent / "eval" / "heldout" / "threads.jsonl").open()
        if line.strip()
    ]
    h04 = next(t for t in heldout if t["thread_id"] == "H-04")
    texts = chatter + [m["text"] for m in h04["messages"]]
    assert len(texts) >= 200
    fired = [t for t in texts if trigger.is_resolution(t)]
    assert fired == []
    assert trigger.is_resolution("Did that. Traffic is back 👍")
    assert trigger.is_resolution_reaction("white_check_mark")
    assert not trigger.is_resolution_reaction("+1")


# --- BE-3.1: gates -----------------------------------------------------------------------


def test_v1_v2_and_r2_on_elisas_thread() -> None:
    g = gates.ground(ELISA_RULE, elisa_thread().messages)
    assert isinstance(g, gates.Grounded)
    assert g.recorded_at == g.valid_from == MSG8  # ST-20: message 8, to the second
    assert {a.value for a in g.draft.avoid} >= {"replace_sfp", "reboot_node"}  # ST-04
    assert g.observed_at == datetime(2025, 10, 14, 6, 21, 30, tzinfo=UTC)  # message 7
    assert g.dropped == []
    assert all(s.quote for s in g.sources)


def test_v1_drops_a_rule_without_an_action_quote() -> None:
    """ST-45"""
    no_quote = ELISA_RULE.model_copy(update={"action": q("bounce_interface", "bounce it")})
    assert gates.ground(no_quote, elisa_thread().messages) == (
        "V1: the action has no verbatim quote"
    )
    assert gates.ground(ELISA_RULE.model_copy(update={"action": None}), []) == "V1: no action"


def test_v2_a_question_is_not_a_rule() -> None:
    guess = ELISA_RULE.model_copy(update={"action": q("replace_sfp", "So another SFP?")})
    assert str(gates.ground(guess, elisa_thread().messages)).startswith("V2")


def test_v1_drops_unquoted_fields_but_keeps_the_rule() -> None:
    draft = ELISA_RULE.model_copy(
        update={"avoid": [*ELISA_RULE.avoid, q("call_vendor", "ring the vendor")]}
    )
    g = gates.ground(draft, elisa_thread().messages)
    assert isinstance(g, gates.Grounded)
    assert "call_vendor" not in {a.value for a in g.draft.avoid}
    assert g.dropped == ["avoid:call_vendor (no verbatim quote)"]


def test_stated_dates_are_resolved_by_code_in_helsinki() -> None:
    """H-02 (future date), H-08 ("from Monday"), and an expiry (R4a)."""
    sent = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)  # a Wednesday
    t = thread_of(
        [
            (
                sent,
                "Pekka",
                "Senior Field Technician",
                "From Monday the Pori room uses a code lock. Ask site security for the code.",
            ),
            (
                sent + timedelta(minutes=1),
                "Pekka",
                None,
                "The old gate stays open until 20 November.",
            ),
        ]
    )
    draft = ELISA_RULE.model_copy(
        update={
            "action": q("get_code", "Ask site security for the code"),
            "conditions": [],
            "avoid": [],
            "observed": [],
            "entities": [],
            "valid_from": StatedTimeOut(phrase="From Monday", tense="future"),
        }
    )
    g = gates.ground(draft, t.messages)
    assert isinstance(g, gates.Grounded)
    assert g.valid_from == datetime(2026, 11, 8, 22, 0, tzinfo=UTC)  # ST-26, EET
    assert g.recorded_at == sent
    expiring = draft.model_copy(
        update={
            "valid_from": None,
            "valid_to": StatedTimeOut(phrase="until 20 November", tense="future"),
        }
    )
    g2 = gates.ground(expiring, t.messages)
    assert isinstance(g2, gates.Grounded)
    assert g2.valid_to == datetime(2026, 11, 19, 22, 0, tzinfo=UTC)


def test_a_date_it_cannot_read_becomes_a_question_not_a_guess() -> None:
    t = thread_of([(MSG8, "Erkki", None, "From Monday or Tuesday, reset the port first.")])
    draft = ELISA_RULE.model_copy(
        update={
            "action": q("bounce_interface", "reset the port first"),
            "conditions": [],
            "avoid": [],
            "observed": [],
            "entities": [],
            "valid_from": StatedTimeOut(phrase="From Monday or Tuesday", tense="future"),
        }
    )
    g = gates.ground(draft, t.messages)
    assert isinstance(g, gates.Grounded)
    assert g.valid_from == MSG8
    assert g.time_note
    assert "Monday or Tuesday" in g.time_note


# --- BE-3.2: linking + V3 ------------------------------------------------------------------


def test_linking_elisas_case_from_the_inventory_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST-10, and it never reads entity_aliases.csv (ST-15)."""

    def no_aliases(self: MockData) -> dict[str, str]:
        raise AssertionError("entity_aliases.csv is scoring-only")

    monkeypatch.setattr(MockData, "model_aliases", no_aliases)
    t = elisa_thread()
    linked = link(ELISA_RULE.entities, t.text(), TriageKind.RULE, inventory(), MSG8)
    by_scope = {s: {b.entity_id for b in linked.bindings if b.scope is s} for s in Scope}
    assert by_scope[Scope.OBSERVED_ON] == {"TRN-042"}
    assert by_scope[Scope.APPLIES_TO] >= {"TNC-1X", "Gen1"}
    assert by_scope[Scope.ACTIVITY] == {"TOPIC:sfp_replacement"}
    assert linked.question is None
    assert not any(b.candidate for b in linked.bindings)
    model = next(b for b in linked.bindings if b.entity_id == "TNC-1X")
    assert model.resolution_confidence == 0.9  # "installed around 2019" = TRN-042's year


def test_access_knowledge_binds_the_road_not_the_node() -> None:
    """ST-11: the Kemi barrier is SEG-042 / Kemi, not TRN-042."""
    text = "The Kemi gate has a padlock now, Pekka has the key."
    linked = link([Mention(surface_form="Kemi gate")], text, TriageKind.ACCESS, inventory(), MSG8)
    ids = {b.entity_id for b in linked.bindings}
    assert "SEG-042" in ids
    assert "TRN-042" not in ids


def test_two_devices_at_one_place_ask_one_question() -> None:
    """ST-06: "the Kemi box" with two transport nodes at Kemi."""
    inv = inventory()
    twin = {**next(e for e in inv if e["entity_id"] == "TRN-042"), "entity_id": "TRN-142"}
    linked = link(
        [Mention(surface_form="the Kemi box")],
        "reset the Kemi box",
        TriageKind.RULE,
        [*inv, twin],
        MSG8,
    )
    candidates = {b.entity_id for b in linked.bindings if b.candidate}
    assert candidates == {"TRN-042", "TRN-142"}
    assert linked.question == 'Which one is "the Kemi box"?'
    assert {o.entity_id for o in linked.options} == candidates


def test_v3_an_entity_that_did_not_exist_yet_is_a_candidate() -> None:
    before = datetime(2018, 6, 1, tzinfo=UTC)  # TRN-042 is commissioned 2019-04-18
    linked = link([], "TRN-042 again", TriageKind.RULE, inventory(), before)
    (b,) = linked.bindings
    assert b.entity_id == "TRN-042"
    assert b.candidate
    assert linked.question
    assert "did not exist" in linked.question


def test_ports_named_in_the_thread() -> None:
    assert ports("bounce 1/1/3 then check 1/1/3 and 1/2/10") == ["1/1/3", "1/2/10"]


# --- BE-3.3: reconcile ---------------------------------------------------------------------

T1 = datetime(2026, 8, 3, tzinfo=UTC)


def _cand(aid: str, thread: str = "other", vf: datetime = T1, **kw: Any) -> dict[str, Any]:
    return {"assertion_id": aid, "threads": [thread], "valid_from": vf, "recorded_at": vf, **kw}


def _decide(
    rel: str, cand: dict[str, Any], vf: datetime = T1 + timedelta(days=1)
) -> reconcile.Decision:
    return reconcile.decide(
        thread_id="t1",
        valid_from=vf,
        candidates=[cand],
        relations=[Relation(assertion_id=cand["assertion_id"], relation=rel)],
    )


def test_reconcile_decisions() -> None:
    assert _decide("duplicate", _cand("A", thread="t1")).skip_as == "A"  # re-triage
    assert _decide("duplicate", _cand("A")).corroborates == ["A"]  # ST-02: not a copy
    assert _decide("corroborates", _cand("A")).corroborates == ["A"]
    assert _decide("supersedes", _cand("A")).supersede == "A"  # ST-22, ST-27
    assert _decide("supersedes", _cand("A"), vf=T1).contradicts == ["A"]  # not later
    assert _decide("corrects", _cand("A", thread="t1")).correct == "A"
    assert _decide("corrects", _cand("A")).contradicts == ["A"]
    assert _decide("contradicts", _cand("A")).contradicts == ["A"]
    assert _decide("unrelated", _cand("A")) == reconcile.Decision()


def test_authority_ranks_recency_then_role_then_channel() -> None:
    assert reconcile.channel_authority("itsm") < reconcile.channel_authority("slack")
    assert reconcile.channel_authority("email") == reconcile.channel_authority("teams")
    newer = reconcile.rank_key(T1 + timedelta(days=1), None, "itsm")
    expert = reconcile.rank_key(T1, "Network Expert", "slack")
    tech = reconcile.rank_key(T1, "Field Technician", "slack")
    kb = reconcile.rank_key(T1, "Network Expert", "itsm")
    assert newer > expert > tech
    assert expert > kb


# --- BE-2.1: Slack events ------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.events: dict[str, MessageIn] = {}
        self.cursors: dict[str, str] = {}
        self.resolved: list[tuple[str, bool]] = []

    async def store(self, messages: list[MessageIn], now: datetime) -> int:
        new = [m for m in messages if m.event_id not in self.events]
        self.events.update({m.event_id: m for m in new})
        return len(new)

    async def cursor(self, channel: str) -> str | None:
        return self.cursors.get(channel)

    async def advance_cursor(self, channel: str, ts: str) -> None:
        self.cursors[channel] = max(self.cursors.get(channel, ""), ts)

    async def mark_resolved(self, event_id: str, at: datetime, *, forced: bool = False) -> bool:
        self.resolved.append((event_id, forced))
        return event_id in self.events


def _ingest(**kw: Any) -> tuple[SlackIngest, _Store]:
    store = _Store()
    return SlackIngest(store, workspace_url="https://x.slack.com/", **kw), store  # type: ignore[arg-type]


MSG = {
    "type": "message",
    "channel": "C1",
    "user": "U1",
    "ts": "1760422928.000100",
    "text": "Disable the interface",
}


async def test_message_edit_and_delete_are_events_and_retries_are_not() -> None:
    ingest, store = _ingest()
    first = await ingest.on_message(MSG)
    assert first is not None
    assert first.event_id == "slack:C1:1760422928.000100"
    assert first.event_time == datetime(2025, 10, 14, 6, 22, 8, 100, tzinfo=UTC)  # R1
    assert first.permalink == "https://x.slack.com/archives/C1/p1760422928000100"
    assert await ingest.on_message(MSG) is None  # ST-61: a redelivery
    edit = await ingest.on_message(
        {
            "channel": "C1",
            "subtype": "message_changed",
            "ts": "1760423000.000000",
            "message": {
                **MSG,
                "text": "Disable it, then enable",
                "edited": {"ts": "1760423000.000000"},
            },
        }
    )
    assert edit is not None
    assert edit.edit_of == first.event_id
    assert edit.event_time > first.event_time
    gone = await ingest.on_message(
        {
            "channel": "C1",
            "subtype": "message_deleted",
            "ts": "1760423100.000000",
            "deleted_ts": MSG["ts"],
            "previous_message": MSG,
        }
    )
    assert gone is not None
    assert gone.deletes == first.event_id
    assert len(store.events) == 3
    assert store.cursors["C1"] == "1760423100.000000"


@pytest.mark.parametrize(
    "event",
    [
        {**MSG, "bot_id": "B1"},
        {**MSG, "subtype": "channel_join"},
        {**MSG, "channel_type": "im"},
        {**MSG, "channel": "CLOG"},
    ],
)
async def test_bots_joins_dms_and_the_log_channel_are_not_captured(event: dict[str, Any]) -> None:
    ingest, store = _ingest(skip_channels={"CLOG"})
    assert await ingest.on_message(event) is None
    assert store.events == {}


async def test_capture_channels_narrow_on_purpose() -> None:
    ingest, _ = _ingest(channels={"C2"})
    assert await ingest.on_message(MSG) is None
    ingest, _ = _ingest()
    assert await ingest.on_message(MSG) is not None  # unset: every channel it is in


async def test_a_resolution_reaction_resolves_the_thread() -> None:
    ingest, _ = _ingest()
    await ingest.on_message(MSG)
    reaction = {
        "reaction": "tada",
        "item": {"channel": "C1", "ts": MSG["ts"]},
        "event_ts": "1760423200.0",
    }
    assert await ingest.on_reaction({**reaction, "event_ts": "1760423200.000000"})
    assert not await ingest.on_reaction({**reaction, "reaction": "+1"})


class _History:
    def __init__(
        self, history: list[dict[str, Any]], replies: dict[str, list[dict[str, Any]]]
    ) -> None:
        self.history, self.replies = history, replies
        self.oldest: list[str] = []

    async def conversations_history(self, **kw: Any) -> dict[str, Any]:
        self.oldest.append(kw["oldest"])
        return {"messages": [m for m in self.history if m["ts"] > kw["oldest"]]}

    async def conversations_replies(self, **kw: Any) -> dict[str, Any]:
        return {"messages": self.replies[kw["ts"]]}


async def test_catch_up_reads_from_the_watermark_and_is_idempotent() -> None:
    """ST-08: posted while the bot was down, including a reply to an older thread."""
    parent: dict[str, Any] = {
        "user": "U1",
        "ts": "1760000000.000000",
        "text": "TRN-042 no traffic",
        "reply_count": 2,
        "latest_reply": "1760422928.000000",
    }
    replies = [
        parent,
        {"user": "U2", "ts": "1760422800.000000", "thread_ts": parent["ts"], "text": "bounce it"},
        {
            "user": "U1",
            "ts": "1760422928.000000",
            "thread_ts": parent["ts"],
            "text": "Traffic is back",
        },
    ]
    client = _History([parent], {parent["ts"]: replies})
    ingest, store = _ingest(channels={"C1"})
    store.cursors["C1"] = "1760422700.000000"  # the bot saw messages up to here
    assert await ingest.catch_up(client) == 3  # type: ignore[arg-type]
    assert client.oldest[0] < parent["ts"]  # parents within the lookback are re-read
    assert {m.thread_id for m in store.events.values()} == {f"slack:C1:{parent['ts']}"}
    assert any(m.resolution for m in store.events.values())
    assert await ingest.catch_up(client) == 0  # type: ignore[arg-type]  # a second restart


# --- SL-1: the card ------------------------------------------------------------------------


def _view(**kw: Any) -> AssertionView:
    base: dict[str, Any] = {
        "assertion_id": "ASR-X",
        "statement": "If the interface is UP with zero counters, bounce it.",
        "status": AssertionStatus.UNVERIFIED,
        "valid_from": MSG8,
        "recorded_at": MSG8,
        "qualifiers": {"avoid": ["replace_sfp", "reboot_node"], "telemetry": "agrees"},
        "bindings": [
            Binding(entity_id="TNC-1X", scope=Scope.APPLIES_TO),
            Binding(entity_id="Gen1", scope=Scope.APPLIES_TO),
            Binding(entity_id="TRN-042", scope=Scope.OBSERVED_ON),
            Binding(entity_id="TOPIC:sfp_replacement", scope=Scope.ACTIVITY),
        ],
        "citations": [Citation(source_type="slack", author="U0ERKKI", recorded_at=MSG8)],
    }
    return AssertionView.model_validate(base | kw)


THREAD = ThreadView(thread_id="slack:C1:1.0", channel="C1", last_message_ts=MSG8)


def test_card_shows_scope_rule_avoid_validity_source_and_optional_buttons() -> None:
    text, blocks = cap.card_blocks([_view()], THREAD, {"ASR-X": Counter({"email": 1})})
    rendered = json.dumps(blocks, ensure_ascii=False)
    assert "Captured for TNC-1X (Gen1) · seen on TRN-042 · after SFP replacement" in rendered
    assert "replacing the SFP again, rebooting the unit" in rendered
    assert "<@U0ERKKI> (this thread)" in rendered
    assert "telemetry agrees ✓" in rendered
    assert "+1 source (email)" in rendered  # ST-02
    assert "unverified, in use" in rendered  # ST-07
    buttons = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert buttons == [cap.CARD_CONFIRM, cap.CARD_FIX, cap.CARD_RETRACT]
    assert "Captured:" in text


def test_card_asks_its_one_question_inside_the_card() -> None:
    v = _view(
        bindings=[
            Binding(entity_id="TRN-042", scope=Scope.OBSERVED_ON, candidate=True),
            Binding(entity_id="TRN-142", scope=Scope.OBSERVED_ON, candidate=True),
        ],
        qualifiers={"question": 'Which one is "the Kemi box"?'},
    )
    _, blocks = cap.card_blocks([v], THREAD, {})
    binds = [
        e
        for b in blocks
        if b["type"] == "actions"
        for e in b["elements"]
        if e["action_id"] == cap.CARD_BIND
    ]
    assert [json.loads(e["value"])["e"] for e in binds] == ["TRN-042", "TRN-142"]
    confirmed = cap.card_blocks([_view(status=AssertionStatus.BELIEVED_TRUE)], THREAD, {})[1]
    assert not [b for b in confirmed if b["type"] == "actions"]  # done: no buttons at all
    disputed = cap.card_blocks([_view(status=AssertionStatus.DISPUTED)], THREAD, {})[1]
    assert cap.CARD_CONFIRM not in json.dumps(disputed)
    assert cap.CARD_FIX in json.dumps(disputed)
    assert cap.CARD_RETRACT in json.dumps(disputed)
    removed = cap.card_blocks([_view(retracted_at=MSG8)], THREAD, {})[1]
    assert "Not knowledge: removed" in json.dumps(removed)


def test_st15_entity_linking_precision_and_recall_on_the_alias_table() -> None:
    """ST-15: P and R >= 0.9 on kind=entity aliases, scored with a file the linker never
    reads."""
    from silentctx.eval.linking import score_aliases

    aliases = MOCK_DATA / "knowledge_graph" / "entity_aliases.csv"
    score = score_aliases(aliases, inventory(), datetime(2026, 11, 7, tzinfo=UTC))
    assert score.rows == 32
    assert score.precision >= 0.9, score.wrong
    assert score.recall >= 0.9, score.misses


def test_model_ids_and_old_units_resolve_from_the_inventory() -> None:
    def ids(text: str) -> set[str]:
        return {
            b.entity_id
            for b in link(
                [Mention(surface_form=text)], text, TriageKind.RULE, inventory(), MSG8
            ).bindings
        }

    assert ids("tnc2_roadm") == {"TNC-2X", "Gen2"}
    assert ids("Gen3 units") == {"TNC-3X", "Gen3"}
    assert ids("the older units") == {"TNC-1X", "Gen1"}  # first installed
    assert "KMAX-4H" not in ids("KMAX-4K radio")  # a shared prefix needs the full id
    assert ids("private road to the Kemi transport room") == {"SEG-042"}


async def test_catch_up_lists_private_channels_and_falls_back_without_the_scope() -> None:
    from slack_sdk.errors import SlackApiError

    class Lister:
        def __init__(self, scoped: bool) -> None:
            self.scoped, self.types = scoped, []

        async def conversations_list(self, **kw: Any) -> dict[str, Any]:
            self.types.append(kw["types"])
            if "private" in kw["types"] and not self.scoped:
                resp = {"ok": False, "error": "missing_scope"}
                raise SlackApiError("missing_scope", resp)
            return {"channels": [{"id": "C1", "is_member": True}, {"id": "G1", "is_member": True}]}

    ingest, _ = _ingest()
    scoped = Lister(scoped=True)
    assert await ingest.watched_channels(scoped) == ["C1", "G1"]  # type: ignore[arg-type]
    assert scoped.types == ["public_channel,private_channel"]
    old = Lister(scoped=False)
    assert await ingest.watched_channels(old) == ["C1", "G1"]  # type: ignore[arg-type]
    assert old.types == ["public_channel,private_channel", "public_channel"]


async def test_the_model_resolves_a_reply_the_word_list_missed() -> None:
    """BE-2.4: the FAST_MODEL classifier, only for thread replies, never blocking."""
    import asyncio

    from silentctx.slack.capture import CaptureHandlers

    class Loop:
        kicks = 0

        def kick(self) -> None:
            Loop.kicks += 1

    asked: list[str] = []

    async def resolves(text: str, start: str) -> bool:
        asked.append(text)
        return "kiitos" in text and "no traffic" in start

    ingest, store = _ingest()

    async def thread(thread_id: str) -> ThreadView:
        root = store.events[f"slack:C1:{MSG['ts']}"]
        return ThreadView(
            thread_id=thread_id,
            channel="C1",
            last_message_ts=root.event_time,
            messages=[MessageView.model_validate(root.model_dump())],
        )

    store.thread = thread  # type: ignore[attr-defined]
    h = CaptureHandlers(ingest, Loop(), resolves)  # type: ignore[arg-type]
    root = {**MSG, "text": "TRN-017 no traffic"}
    await h.on_message(root)  # a new thread: not asked
    reply = {**MSG, "ts": "1760422999.000000", "thread_ts": MSG["ts"], "text": "ok now, kiitos"}
    await h.on_message(reply)
    await asyncio.gather(*h._checks)
    assert asked == ["ok now, kiitos"]
    assert store.resolved == [("slack:C1:1760422999.000000", False)]
    assert Loop.kicks == 1
    await h.on_message({**reply, "ts": "1760423001.000000", "text": "Traffic is back"})
    assert asked == ["ok now, kiitos"]  # the word list was enough
    assert Loop.kicks == 2


def test_a_year_dates_the_equipment_not_the_knowledge() -> None:
    """Live finding: the model passed "installed around 2019" as the rule's start."""
    draft = ELISA_RULE.model_copy(
        update={"valid_from": StatedTimeOut(phrase="installed around 2019", tense="past")}
    )
    g = gates.ground(draft, elisa_thread().messages)
    assert isinstance(g, gates.Grounded)
    assert g.valid_from == MSG8
    assert "valid_from:'installed around 2019' (names no day)" in g.dropped


def test_v2_backstop_puts_a_rejected_suggestion_in_avoid() -> None:
    """ST-04 even when the model forgets it (seen once in three live runs)."""
    forgetful = ELISA_RULE.model_copy(update={"avoid": [ELISA_RULE.avoid[1]]})
    g = gates.ground(forgetful, elisa_thread().messages)
    assert isinstance(g, gates.Grounded)
    assert {a.value for a in g.draft.avoid} == {"replace_sfp", "reboot_node"}
    assert gates.rejected_suggestions(elisa_thread().messages)[0].quote == "So another SFP?"


def test_llm_list_answers_are_unwrapped_instead_of_retried() -> None:
    """Live finding: Kimi returned the rules as a bare list in 2 of 3 runs (+15-30 s)."""
    from silentctx.extraction.llm import _unwrap
    from silentctx.extraction.models import Extraction, Triage

    rule = ELISA_RULE.model_dump()
    assert Extraction.model_validate(_unwrap(Extraction, [rule])).rules[0].topic == rule["topic"]
    wrapped = _unwrap(Extraction, [{"rules": [rule]}])
    assert len(Extraction.model_validate(wrapped).rules) == 1
    triage = {"relevant": True, "kind": "rule", "reason": "x"}
    assert _unwrap(Triage, triage) == triage


async def test_replay_personas_are_people_but_the_bots_own_posts_are_not() -> None:
    """002 §3.6: the demo replay posts as personas through this bot."""
    ingest, _ = _ingest(persona_bot_id="B_US")
    persona = {
        **MSG,
        "user": None,
        "bot_id": "B_US",
        "subtype": "bot_message",
        "username": "Erkki Expert",
    }
    msg = await ingest.on_message(persona)
    assert msg is not None
    assert msg.author == "Erkki Expert"
    card = {**MSG, "ts": "1760422999.000000", "user": None, "bot_id": "B_US"}
    assert await ingest.on_message(card) is None  # our own card or answer
    other = {**persona, "ts": "1760423000.000000", "bot_id": "B_OTHER"}
    assert await ingest.on_message(other) is None  # someone else's bot


def test_a_rejected_parse_falls_back_to_the_raw_answer() -> None:
    from langchain_core.messages import AIMessage

    from silentctx.extraction.llm import _parsed, _unwrap
    from silentctx.extraction.models import Extraction

    rule = ELISA_RULE.model_dump(mode="json")
    raw = AIMessage(content="```json\n" + json.dumps([{"rules": [rule]}]) + "\n```")
    out = {"raw": raw, "parsed": None, "parsing_error": ValueError("list")}
    assert len(Extraction.model_validate(_unwrap(Extraction, _parsed(out))).rules) == 1
    assert _parsed({"raw": raw, "parsed": {"rules": []}}) == {"rules": []}


def test_when_no_mention_resolves_the_thread_text_still_links_the_place() -> None:
    text = "Locked barrier at the start of the private road to the Kemi transport room."
    linked = link([Mention(surface_form="key box")], text, TriageKind.ACCESS, inventory(), MSG8)
    assert {b.entity_id for b in linked.bindings} == {"SEG-042"}


def test_a_supersession_minutes_later_in_the_same_thread_is_a_disagreement() -> None:
    """Live finding on the KMAX-4K pair; H-07 (weeks apart) still supersedes."""
    minutes = _decide("supersedes", _cand("A", thread="t1"), vf=T1 + timedelta(minutes=12))
    assert minutes.contradicts == ["A"]
    assert minutes.supersede is None
    assert (
        _decide("supersedes", _cand("A", thread="t1"), vf=T1 + timedelta(weeks=7)).supersede == "A"
    )


def test_do_x_against_avoid_x_is_a_contradiction_whatever_the_judge_says() -> None:
    """Live finding on the KMAX-4K pair: the judge missed it in one run."""
    juhani = _cand("J", thread="t1", qualifiers_json=json.dumps({"avoid": ["power_cycle_radio"]}))
    d = reconcile.decide(
        thread_id="t1",
        valid_from=T1,
        candidates=[juhani],
        relations=[Relation(assertion_id="J", relation="unrelated")],
        action="power_cycle_radio",
    )
    assert d.contradicts == ["J"]
    heikki = _cand("H", qualifiers_json=json.dumps({"action": "power_cycle_radio"}))
    d2 = reconcile.decide(
        thread_id="t1",
        valid_from=T1,
        candidates=[heikki],
        relations=[],
        action="export_config",
        avoid=["power_cycle_radio"],
    )
    assert d2.contradicts == ["H"]


def test_same_action_from_the_same_thread_is_the_rule_already_captured() -> None:
    """Live finding: after messages 10-12 the judge didn't call the re-read a duplicate."""
    mine = _cand("A", thread="t1", qualifiers_json=json.dumps({"action": "bounce_interface"}))
    d = reconcile.decide(
        thread_id="t1",
        valid_from=T1,
        candidates=[mine],
        relations=[Relation(assertion_id="A", relation="corroborates")],
        action="bounce_interface",
    )
    assert d.skip_as == "A"
    fixed = reconcile.decide(
        thread_id="t1",
        valid_from=T1,
        candidates=[mine],
        relations=[Relation(assertion_id="A", relation="corrects")],
        action="bounce_interface",
    )
    assert fixed.correct == "A"


def test_a_node_the_simulator_does_not_run_adds_no_live_samples() -> None:
    """Live finding: TRN-042 isn't simulated, so the sim returns [] for it, and DuckDB
    rejected the empty insert, failing the whole capture."""
    from silentctx.telemetry.store import TelemetryStore

    store = TelemetryStore(None)
    store.add_live([], replace="TRN-042")
    assert store.ports("TRN-042") == []


def test_card_says_who_fixed_it() -> None:
    """A saved ✏️ Fix left the card looking unchanged; it now says who fixed it."""
    fix = Citation(
        source_type="slack",
        source_ref="card:ASR-X:fix:V1",
        author="U0ALEXIS",
        recorded_at=MSG8 + timedelta(minutes=5),
    )
    v = _view(citations=[*_view().citations, fix])
    rendered = json.dumps(cap.card_blocks([v], THREAD, {})[1])
    assert "fixed by <@U0ALEXIS>" in rendered
    assert "<@U0ERKKI> (this thread)" in rendered  # the source stays the expert
