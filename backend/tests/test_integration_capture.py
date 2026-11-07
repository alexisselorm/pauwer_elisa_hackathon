"""The Slack capture loop end to end against a real Neo4j (testcontainers): ingestion,
thread memory, the extraction graph with gates and reconcile, and the context card.
The LLM is a fake that answers like a good model; everything else is the real code.
Run: uv run pytest -m integration"""

# ruff: noqa: PT009  # `assert_` is the repository method, not unittest

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from neo4j import AsyncDriver

from kaiku.config import Settings
from kaiku.context.models import AssertionIn, Match, Scope
from kaiku.domain.models import Platform
from kaiku.domain.time import datetime_to_slack_ts
from kaiku.extraction.graph import Extractor
from kaiku.extraction.llm import ExtractionError
from kaiku.extraction.models import Mention, RuleDraft, StatedTimeOut, TriageKind
from kaiku.graph import schema
from kaiku.graph.driver import create_driver
from kaiku.graph.repository import ContextRepository
from kaiku.ingest.backfill import backfill, elisa_example
from kaiku.ingest.capture import CaptureLoop
from kaiku.ingest.slack_events import SlackIngest
from kaiku.ingest.store import CaptureStore, MessageIn
from kaiku.seed.profiles import Profile
from kaiku.seed.service import SeedRequest, seed
from kaiku.seed.sources import MockData
from kaiku.slack import capture as cap
from kaiku.telemetry.store import TelemetryStore
from tests.capture_helpers import ELISA_DIR, ELISA_RULE, MOCK_DATA, FakeJudge, q
from tests.test_integration_neo4j import PASSWORD, neo4j_url  # noqa: F401  # shared container

pytestmark = pytest.mark.integration

CHANNEL = "CNOC"
USERS = {"Veikko Vikkelä": "U0VEIKKO", "Erkki Expert": "U0ERKKI"}
MSG8 = datetime(2025, 10, 14, 6, 22, 8, tzinfo=UTC)
_seeded: set[str] = set()


@pytest.fixture
async def driver(
    neo4j_url: str,  # noqa: F811
    make_settings: Callable[..., Settings],
) -> AsyncIterator[AsyncDriver]:
    settings = make_settings(neo4j_uri=neo4j_url, neo4j_password=PASSWORD, data_dir=MOCK_DATA)
    await schema.apply_migrations(settings)
    if neo4j_url not in _seeded:
        await seed(settings, SeedRequest(network=True, profile=Profile.DEMO))
        _seeded.add(neo4j_url)
    d = create_driver(settings)
    await d.execute_query(
        "MATCH (n) WHERE n:Message OR n:Thread OR n:ChannelCursor OR n:CardAction "
        "OR n:Outcome OR n:Person OR n:Email OR (n:ContextAssertion AND n.origin <> 'gold') "
        "OR n.test_capture = true DETACH DELETE n"
    )
    try:
        yield d
    finally:
        await d.close()


class FakeSlack:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.views: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kw: Any) -> dict[str, Any]:
        self.posted.append(kw)
        return {"ts": f"1800000000.{len(self.posted):06d}"}

    async def chat_update(self, **kw: Any) -> dict[str, Any]:
        self.updated.append(kw)
        return {"ok": True}

    async def conversations_replies(self, **kw: Any) -> dict[str, Any]:
        mine = [
            {"ts": f"1800000000.{i:06d}", "bot_id": "BSELF", "text": p["text"]}
            for i, p in enumerate(self.posted, 1)
            if p["channel"] == kw["channel"] and p.get("thread_ts") == kw["ts"]
        ]
        return {"messages": mine}

    async def views_open(self, **kw: Any) -> dict[str, Any]:
        self.views.append(kw)
        return {"ok": True}

    def in_channel(self, channel: str) -> list[dict[str, Any]]:
        return [p for p in self.posted if p["channel"] == channel]


class Rig:
    """The slackbot's capture wiring, with a fake Slack client and a fake LLM."""

    def __init__(self, driver: AsyncDriver, judge: FakeJudge) -> None:
        self.repo = ContextRepository(driver, "neo4j")
        self.store = CaptureStore(driver, "neo4j")
        self.judge = judge
        self.slack = FakeSlack()
        self.cards = cap.CaptureCards(self.store, self.repo, log_channel="CLOG")
        self.cards.client = self.slack  # type: ignore[assignment]
        extractor = Extractor(self.repo, judge, TelemetryStore(MOCK_DATA))
        self.loop = CaptureLoop(self.store, extractor, self.cards.announce)
        self.ingest = SlackIngest(self.store, workspace_url="https://elisa.slack.com")
        self.handlers = cap.CaptureHandlers(self.ingest, self.loop)

    async def q(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        records, _, _ = await self.repo._driver.execute_query(cypher, params)
        return [dict(r) for r in records]


def slack_events() -> list[dict[str, Any]]:
    """Elisa's 12 messages as Slack events in one thread of #noc-transport."""
    chat = [m for m in elisa_example(ELISA_DIR) if m.thread_id == "elisa:teams_chat"]
    root = datetime_to_slack_ts(chat[0].event_time)
    out = []
    for m in chat:
        ts = datetime_to_slack_ts(m.event_time)
        ev = {"type": "message", "channel": CHANNEL, "user": USERS[m.author or ""], "ts": ts}
        out.append({**ev, "text": m.text, **({"thread_ts": root} if ts != root else {})})
    return out


async def _capture_elisa(rig: Rig) -> str:
    events = slack_events()
    for ev in events[:9]:
        await rig.handlers.on_message(ev)
    results = await rig.loop.sweep_once()
    assert [r.status for r in results] == ["captured"], results
    (aid,) = results[0].assertion_ids
    return aid


async def test_elisas_thread_becomes_one_card_and_one_assertion(driver: AsyncDriver) -> None:
    """ST-01, ST-10, ST-20, ST-40, ST-09 (no re-triage without news), BE-2.4 memory."""
    rig = Rig(driver, FakeJudge(relations={"*": "duplicate"}))
    aid = await _capture_elisa(rig)

    (a,) = await rig.repo.assertions([aid])
    assert a.recorded_at == a.valid_from == MSG8  # ST-20: Slack ts of message 8
    assert a.valid_to is None
    assert a.status == "unverified"
    by_scope = {s: {b.entity_id for b in a.bindings if b.scope is s} for s in Scope}
    assert by_scope[Scope.OBSERVED_ON] == {"TRN-042"}
    assert by_scope[Scope.APPLIES_TO] >= {"TNC-1X", "Gen1"}
    assert by_scope[Scope.ACTIVITY] == {"TOPIC:sfp_replacement"}
    assert a.qualifiers["action"] == "bounce_interface"
    assert set(a.qualifiers["avoid"]) >= {"replace_sfp", "reboot_node"}
    assert set(a.qualifiers["symptom"]) == {"oper_up", "rx_power_normal", "counters_zero"}
    assert a.qualifiers["telemetry"] == "agrees"
    assert a.support_n == 1  # V4: SUPPORTS from the network, 06:00 bin (ST-40)
    net = await rig.q(
        "MATCH (o:Outcome {platform: 'network'})-[:SUPPORTS]->(:ContextAssertion "
        "{assertion_id: $id}) RETURN o.port AS port, o.event_time AS t",
        id=aid,
    )
    assert [r["port"] for r in net] == ["1/1/3"]
    assert any(c.source_ref and "/archives/CNOC/p" in c.source_ref for c in a.citations)

    thread = await rig.store.thread(f"slack:{CHANNEL}:{slack_events()[0]['ts']}")
    assert thread.status == "captured"
    assert thread.assertion_ids == [aid]
    card = rig.slack.in_channel(CHANNEL)
    assert len(card) == 1  # one bot message
    assert card[0]["thread_ts"] == slack_events()[0]["ts"]
    assert "telemetry agrees ✓" in json.dumps(card[0]["blocks"], ensure_ascii=False)
    reason = await rig.q("MATCH (t:Thread) RETURN t.triage_reason AS r, t.triage_kind AS k")
    assert reason == [{"r": "symptom -> action rule", "k": "rule"}]

    # Messages 10-12 arrive: re-triaged once, the same knowledge, the card edited in place.
    for ev in slack_events()[9:]:
        await rig.handlers.on_message(ev)
    await rig.loop.sweep_once()
    assert await rig.q("MATCH (a:ContextAssertion) RETURN count(a) AS n") == [{"n": 1}]
    assert len(rig.slack.in_channel(CHANNEL)) == 1  # ST-01: still ≤1 bot message

    # The graph loses the card's ts (a reset re-captures the thread): the card in Slack is
    # found and edited, never posted twice.
    await rig.q("MATCH (t:Thread) REMOVE t.card_ts")
    await rig.cards.refresh(thread.thread_id)
    assert len(rig.slack.in_channel(CHANNEL)) == 1
    assert rig.slack.updated[-1]["ts"] == "1800000000.000001"
    assert (await rig.store.thread(thread.thread_id)).card_ts == "1800000000.000001"
    assert rig.slack.updated
    calls = list(rig.judge.calls)
    assert await rig.loop.sweep_once() == []  # nothing new: no LLM call at all
    assert rig.judge.calls == calls


async def test_a_retry_and_a_restart_change_nothing(driver: AsyncDriver) -> None:
    """ST-61, ST-08: redelivered events and the same messages again from the catch-up."""
    rig = Rig(driver, FakeJudge())
    for ev in slack_events():
        await rig.handlers.on_message(ev)
        await rig.handlers.on_message(ev)
    counts = await rig.q("MATCH (m:Message) RETURN count(m) AS n")
    assert counts == [{"n": 12}]
    cursor = await rig.store.cursor(CHANNEL)
    assert cursor == slack_events()[-1]["ts"]


async def test_bot_down_catch_up_captures_the_same_single_assertion(driver: AsyncDriver) -> None:
    """ST-08: the thread was posted while the bot was down."""
    events = slack_events()
    root, replies = events[0], events[1:]

    class History:
        async def conversations_list(self, **kw: Any) -> dict[str, Any]:
            return {"channels": [{"id": CHANNEL, "is_member": True}]}

        async def conversations_history(self, **kw: Any) -> dict[str, Any]:
            return {"messages": [{**root, "reply_count": 11, "latest_reply": replies[-1]["ts"]}]}

        async def conversations_replies(self, **kw: Any) -> dict[str, Any]:
            return {"messages": [root, *replies]}

    rig = Rig(driver, FakeJudge())
    assert await rig.ingest.catch_up(History()) == 12  # type: ignore[arg-type]
    assert await rig.ingest.catch_up(History()) == 0  # type: ignore[arg-type]
    results = await rig.loop.sweep_once()
    assert len(results[0].assertion_ids) == 1
    assert await rig.q("MATCH (a:ContextAssertion) RETURN count(a) AS n") == [{"n": 1}]


async def test_ignored_thread_is_re_triaged_once_when_an_expert_replies(
    driver: AsyncDriver,
) -> None:
    """ST-09: ignored, then a fix arrives in the thread, and it becomes captured. The ignore
    decision goes to #context-log, never to the work channel."""
    rig = Rig(driver, FakeJudge(relevant=False))
    events = slack_events()
    for ev in events[:2]:
        await rig.handlers.on_message(ev)
    (first,) = await rig.loop.sweep_once()
    assert first.status == "ignored"
    assert rig.slack.in_channel(CHANNEL) == []
    assert "small talk" in rig.slack.in_channel("CLOG")[0]["text"]
    assert await rig.loop.sweep_once() == []

    rig.judge.relevant = True
    for ev in events[2:9]:
        await rig.handlers.on_message(ev)
    (second,) = await rig.loop.sweep_once()
    assert second.status == "captured"
    assert rig.judge.calls.count("triage") == 2


async def test_an_llm_outage_parks_the_thread_and_retries_it(driver: AsyncDriver) -> None:
    """BE-3.1 off-nominal, ST-60: nothing half-written; the next sweep tries again."""
    rig = Rig(driver, FakeJudge(fail=ExtractionError("timeout")))
    for ev in slack_events()[:9]:
        await rig.handlers.on_message(ev)
    (r,) = await rig.loop.sweep_once()
    assert r.status == "extraction_failed"
    assert await rig.q("MATCH (a:ContextAssertion) RETURN count(a) AS n") == [{"n": 0}]
    assert (await rig.q("MATCH (t:Thread) RETURN t.status AS s")) == [{"s": "extraction_failed"}]
    rig.judge.fail = None
    (again,) = await rig.loop.sweep_once()
    assert again.status == "captured"


async def test_email_corroborates_and_the_card_gains_a_source(driver: AsyncDriver) -> None:
    """ST-02: no new rule from the email, CORROBORATES, "+1 source (email)" on the card,
    and the email capture is logged in #context-log, not posted to a work channel."""
    rig = Rig(driver, FakeJudge(relations={"*": "duplicate"}))
    aid = await _capture_elisa(rig)
    email = next(m for m in elisa_example(ELISA_DIR) if m.platform == "email")
    await rig.store.store([email], datetime.now(UTC))
    rig.judge.rules = [
        ELISA_RULE.model_copy(
            update={
                "action": q("bounce_interface", "try disabling and enabling the interface once"),
                "conditions": [q("counters_zero", "the counters remain at zero")],
                "avoid": [q("replace_sfp", "Before replacing the SFP again or rebooting")],
                "observed": [],
                "entities": [Mention(surface_form="TRN-042", role=Scope.OBSERVED_ON)],
            }
        )
    ]
    (r,) = await rig.loop.sweep_once()
    assert r.status == "captured"
    rows = await rig.q(
        "MATCH (e:ContextAssertion)-[:CORROBORATES]-(s:ContextAssertion {assertion_id: $id}) "
        "RETURN e.status AS status",
        id=aid,
    )
    assert rows == [{"status": "unverified"}]
    assert len(rig.slack.in_channel(CHANNEL)) == 1
    assert "+1 source (email)" in json.dumps(rig.slack.updated[-1]["blocks"])
    assert "Captured from email" in rig.slack.in_channel("CLOG")[0]["text"]


async def _thread(
    rig: Rig, thread_id: str, rows: list[tuple[datetime, str, str, str]], platform: str = "slack"
) -> None:
    await rig.store.store(
        [
            MessageIn(
                event_id=f"{thread_id}:{i}",
                platform=Platform(platform),
                channel="C2",
                thread_id=thread_id,
                event_time=t,
                author=a,
                author_role=r,
                text=x,
            )
            for i, (t, a, r, x) in enumerate(rows)
        ],
        datetime.now(UTC),
    )


def _rule(statement: str, action: tuple[str, str], entities: list[str], **kw: Any) -> RuleDraft:
    return RuleDraft(
        statement=statement,
        topic=kw.pop("topic", "radio_restart"),
        action=q(*action),
        entities=[Mention(surface_form=e) for e in entities],
        **kw,
    )


async def test_same_author_correcting_themselves_supersedes(driver: AsyncDriver) -> None:
    """H-07, ST-27: SUPERSEDES; predecessor valid_to = successor valid_from."""
    sep, nov = datetime(2026, 9, 15, 9, tzinfo=UTC), datetime(2026, 11, 4, 14, tzinfo=UTC)
    first = "KMAX-4K radio on ACC-007 hanging again, just power-cycle it, it always comes back."
    second = (
        "Correction to what I said in September: since the October firmware a power-cycle "
        "wipes the config on the KMAX-4K. Export the config first, then restart."
    )
    rig = Rig(
        driver,
        FakeJudge(
            [
                _rule(
                    "Power-cycle a hanging KMAX-4K.",
                    ("power_cycle_radio", "just power-cycle it"),
                    ["ACC-007"],
                )
            ]
        ),
    )
    await _thread(rig, "h07", [(sep, "Pekka", "Senior Field Technician", first)])
    (a,) = (await rig.loop.sweep_once())[0].assertion_ids
    await _thread(
        rig,
        "h07",
        [
            (sep, "Pekka", "Senior Field Technician", first),
            (nov, "Pekka", "Senior Field Technician", second),
        ],
    )
    rig.judge.rules = [
        _rule(
            "Export the KMAX-4K config before a restart.",
            ("export_config", "Export the config first, then restart"),
            ["ACC-007"],
        )
    ]
    rig.judge.relations = {"*": "supersedes"}
    (r,) = await rig.loop.sweep_once()
    b = next(i for i in r.assertion_ids if i != a)
    old, new = await rig.repo.assertions([a, b])
    assert old.status == "superseded"
    assert old.valid_to == new.valid_from == nov
    assert new.supersedes == a
    assert {b.entity_id for b in new.bindings} >= {"ACC-007", "KMAX-4K"}
    thread = await rig.store.thread("h07")
    assert thread.assertion_ids == [a, b]


async def test_kemi_code_lock_supersedes_the_padlock_from_its_stated_date(
    driver: AsyncDriver,
) -> None:
    """ST-22: valid_from = 3 August (Helsinki), recorded_at = the email, padlock ends then."""
    rig = Rig(
        driver,
        FakeJudge(
            [
                _rule(
                    "The Kemi gate has a padlock; Pekka has the key.",
                    ("get_key", "Pekka has the key"),
                    ["Kemi gate"],
                    topic="access_keys",
                )
            ],
            kind=TriageKind.ACCESS,
        ),
    )
    await _thread(
        rig,
        "kemi-1",
        [
            (
                datetime(2026, 5, 4, 8, tzinfo=UTC),
                "Pekka",
                "Senior Field Technician",
                "Kemi gate is a padlock, Pekka has the key.",
            )
        ],
        platform="teams",
    )
    (padlock,) = (await rig.loop.sweep_once())[0].assertion_ids
    sent = datetime(2026, 8, 18, 7, tzinfo=UTC)
    await _thread(
        rig,
        "kemi-2",
        [
            (
                sent,
                "Heikki Lehtonen",
                "Contractor Coordinator",
                "The Kemi gate is a code lock, installed 3 August. "
                "Get the code from site security.",
            )
        ],
        platform="email",
    )
    rig.judge.rules = [
        _rule(
            "The Kemi gate is a code lock; site security has the code.",
            ("get_code", "Get the code from site security"),
            ["Kemi gate"],
            topic="access_keys",
            valid_from=StatedTimeOut(phrase="installed 3 August", tense="past"),
        )
    ]
    rig.judge.relations = {"*": "supersedes"}
    (r,) = await rig.loop.sweep_once()
    (code,) = r.assertion_ids
    old, new = await rig.repo.assertions([padlock, code])
    aug3 = datetime(2026, 8, 2, 21, tzinfo=UTC)  # 00:00 EEST
    assert new.valid_from == aug3
    assert new.recorded_at == sent
    assert old.valid_to == aug3
    assert old.status == "superseded"
    assert "SEG-042" in {b.entity_id for b in new.bindings}


async def test_stale_itsm_kb_is_contradicted_but_does_not_dispute_the_expert(
    driver: AsyncDriver,
) -> None:
    """ST-28 (lower authority loses), and two experts disagreeing are both disputed."""
    rig = Rig(driver, FakeJudge(relations={"*": "contradicts"}))
    await rig.q(
        "CREATE (:Event:Document {event_id: 'kb-17', event_time: datetime('2024-03-01T00:00:00Z'), "
        "platform: 'itsm', test_capture: true})"
    )
    kb = await rig.repo.assert_(
        AssertionIn(
            statement="ITSM KB: TRN-042-only known issue; workaround is to reboot the whole node.",
            topic="forwarding_state_after_sfp_swap",
            bindings=[{"entity_id": "TRN-042", "scope": "observed_on"}],
            sources=[{"event_id": "kb-17", "quote": "reboot the whole node"}],
            authority=0,
        )
    )
    aid = await _capture_elisa(rig)
    expert, stale = await rig.repo.assertions([aid, kb.assertion_id])
    assert stale.status == "disputed"
    assert expert.status == "unverified"
    assert expert.contradicts == [kb.assertion_id]

    t = datetime(2026, 6, 1, tzinfo=UTC)
    rig.judge.rules = [
        _rule(
            "Power-cycle a KMAX-4K on an RF alarm.",
            ("power_cycle_radio", "power-cycle the radio"),
            ["KMAX-4K"],
        )
    ]
    rig.judge.relations = {}
    await _thread(
        rig,
        "km-1",
        [(t, "Pekka", "Senior Field Technician", "RF alarm on a KMAX-4K: power-cycle the radio.")],
    )
    (one,) = (await rig.loop.sweep_once())[0].assertion_ids
    rig.judge.rules = [
        _rule(
            "Never power-cycle a KMAX-4K.",
            ("export_config", "export the config first"),
            ["KMAX-4K"],
        )
    ]
    rig.judge.relations = {one: "contradicts"}
    await _thread(
        rig,
        "km-2",
        [
            (
                t + timedelta(days=1),
                "Juhani",
                "Access Engineer",
                "Do not power-cycle a KMAX-4K, export the config first.",
            )
        ],
    )
    (two,) = (await rig.loop.sweep_once())[0].assertion_ids
    x, y = await rig.repo.assertions([one, two])
    assert (x.status, y.status) == ("disputed", "disputed")


async def test_card_buttons_confirm_fix_and_retract(driver: AsyncDriver) -> None:
    """SL-1: ✅ -> believed_true; ✏️ saves a correction (ST-05: retracted at the click, keeps
    valid_from, no SUPERSEDES); 🚫 retracts. Every click edits the one card."""
    rig = Rig(driver, FakeJudge())
    aid = await _capture_elisa(rig)
    thread_id = f"slack:{CHANNEL}:{slack_events()[0]['ts']}"

    async def ack(**kw: Any) -> None:
        acks.append(kw)

    acks: list[dict[str, Any]] = []
    click = {
        "user": {"id": "U0ERKKI"},
        "trigger_id": "tr",
        "actions": [{"value": json.dumps({"a": aid, "t": thread_id}), "action_ts": "1.1"}],
    }
    await rig.cards.on_fix(ack, click, rig.slack)  # type: ignore[arg-type]
    modal = rig.slack.views[0]["view"]
    assert json.loads(modal["private_metadata"]) == {"a": aid, "t": thread_id}

    def state(**fields: str) -> dict[str, Any]:
        return {"values": {k: {"v": {"value": v}} for k, v in fields.items()}}

    bad = {**modal, "id": "V1", "state": state(statement="x", observed_on="TRN-999")}
    await rig.cards.on_fix_submit(ack, click, bad)
    assert acks[-1]["response_action"] == "errors"  # ST-12: unknown entity, nothing written

    view = {
        **modal,
        "id": "V2",
        "state": state(
            statement="Only TRN-042: bounce the port after an SFP swap.", observed_on="TRN-042"
        ),
    }
    await rig.cards.on_fix_submit(ack, click, view)
    rows = await rig.q(
        "MATCH (w:ContextAssertion {assertion_id: $id}) "
        "MATCH (f:ContextAssertion)-[:CORRECTS]->(w) "
        "RETURN w.retracted_at IS NOT NULL AS retracted, f.valid_from = w.valid_from AS same, "
        "EXISTS { (f)-[:SUPERSEDES]-() } AS sup, f.assertion_id AS fixed, f.status AS status",
        id=aid,
    )
    assert rows[0] | {"fixed": None} == {
        "retracted": True,
        "same": True,
        "sup": False,
        "fixed": None,
        "status": "believed_true",
    }
    fixed = rows[0]["fixed"]
    assert (await rig.store.thread(thread_id)).assertion_ids == [fixed]

    click2 = {
        **click,
        "actions": [{"value": json.dumps({"a": fixed, "t": thread_id}), "action_ts": "2.2"}],
    }
    await rig.cards.on_retract(ack, click2)
    (gone,) = await rig.repo.assertions([fixed])
    assert gone.retracted_at is not None
    assert len(rig.slack.in_channel(CHANNEL)) == 1  # still one card, edited each time
    assert "Not knowledge: removed" in json.dumps(rig.slack.updated[-1]["blocks"])
    kinds = await rig.q("MATCH (e:CardAction) RETURN e.action AS a ORDER BY a")
    assert [k["a"] for k in kinds] == ["fix", "retract"]


async def test_confirm_and_the_one_clarifying_question(driver: AsyncDriver) -> None:
    """ST-06: two Kemi nodes -> one question on the card; the answer binds the entity."""
    rig = Rig(driver, FakeJudge())
    await rig.q(
        "MATCH (o:Device {entity_id: 'TRN-042'}) "
        "CREATE (n:Entity:Device {entity_id: 'TRN-142', test_capture: true}) "
        "SET n += o { .city, .entity_type, .valid_from, .model, .name }, n.entity_id = 'TRN-142'"
    )
    t = datetime(2026, 10, 1, 8, tzinfo=UTC)
    ts = datetime_to_slack_ts(t)
    rig.judge.rules = [
        _rule(
            "Reset the Kemi box port after a swap.",
            ("bounce_interface", "reset the port on the Kemi box"),
            ["the Kemi box"],
            topic="forwarding_state_after_sfp_swap",
        )
    ]
    await rig.handlers.on_message(
        {"channel": "C3", "user": "U1", "ts": ts, "text": "Fixed: reset the port on the Kemi box"}
    )
    (r,) = await rig.loop.sweep_once()
    (aid,) = r.assertion_ids
    blocks = json.dumps(rig.slack.posted[0]["blocks"])
    assert "Which one is" in blocks
    assert "TRN-142" in blocks
    thread_id = f"slack:C3:{ts}"

    async def ack(**kw: Any) -> None:
        return None

    body = {
        "user": {"id": "U2"},
        "actions": [
            {"value": json.dumps({"a": aid, "t": thread_id, "e": "TRN-042"}), "action_ts": "3.3"}
        ],
    }
    later = t + timedelta(days=1)

    async def in_context() -> bool:
        views = await rig.repo.context({"TRN-042": Match.DIRECT}, later, later)
        return aid in {v.assertion_id for v in views}

    assert not await in_context()  # a candidate is not knowledge about TRN-042 yet
    await rig.cards.on_bind(ack, body)
    assert await in_context()
    (a,) = await rig.repo.assertions([aid])
    assert [(b.entity_id, b.candidate) for b in a.bindings if b.scope is Scope.OBSERVED_ON] == [
        ("TRN-042", False)
    ]
    assert "Which one is" not in json.dumps(rig.slack.updated[-1]["blocks"])
    await rig.cards.on_confirm(
        ack,
        {
            **body,
            "actions": [{"value": json.dumps({"a": aid, "t": thread_id}), "action_ts": "4.4"}],
        },
    )
    assert (await rig.repo.assertions([aid]))[0].status == "believed_true"
    assert len(rig.slack.posted) == 1


async def test_backfill_is_idempotent_and_respects_the_demo_profile(driver: AsyncDriver) -> None:
    """ST-62, plan 001 §3.5."""
    store = CaptureStore(driver, "neo4j")
    data = MockData(MOCK_DATA)
    demo = await backfill(store, data, Profile.DEMO, elisa_dir=ELISA_DIR)
    assert demo.teams_skipped_demo > 0
    assert demo.elisa_read == 0
    full = await backfill(store, data, Profile.FULL, elisa_dir=ELISA_DIR)
    assert full.new_events == demo.teams_skipped_demo + full.elisa_read
    again = await backfill(store, data, Profile.FULL, elisa_dir=ELISA_DIR)
    assert again.new_events == 0
    records, _, _ = await driver.execute_query(
        "MATCH (m:Message {event_id: 'elisa:teams_chat:8'}) RETURN m.event_time AS t"
    )
    assert records[0]["t"].to_native() == MSG8


async def test_two_experts_disagreeing_in_one_thread_are_both_disputed(
    driver: AsyncDriver,
) -> None:
    """KMAX-4K, as in the corpus: both rules come from ONE conversation, so the second rule
    must be reconciled against the first after it is persisted."""
    t = datetime(2026, 6, 2, 11, tzinfo=UTC)
    rows = [
        (
            t,
            "Heikki Lehtonen",
            "Contractor Coordinator",
            "RF alarm is up on the Kajaani hop. "
            "I usually just power-cycle the radio (the KMAX-4K).",
        ),
        (
            t + timedelta(minutes=12),
            "Juhani Virtanen",
            "Access Engineer",
            "Please do NOT power-cycle KMAX-4K. Export the config first and escalate.",
        ),
    ]
    rig = Rig(driver, FakeJudge(relations={"*": "contradicts"}))
    rig.judge.rules = [
        _rule(
            "Power-cycle a KMAX-4K on an RF alarm.",
            ("power_cycle_radio", "power-cycle the radio"),
            ["KMAX-4K"],
        ),
        _rule(
            "Never power-cycle a KMAX-4K; export the config first.",
            ("export_config", "Export the config first and escalate"),
            ["KMAX-4K"],
        ),
    ]
    await _thread(rig, "kmax", rows)
    (r,) = await rig.loop.sweep_once()
    one, two = r.assertion_ids
    x, y = await rig.repo.assertions([one, two])
    assert (x.status, y.status) == ("disputed", "disputed")
    assert y.contradicts == [one]


async def test_vector_search_finds_a_candidate_topic_and_entity_would_miss(
    driver: AsyncDriver,
) -> None:
    """BE-3.3: candidates include the vector hits (real Neo4j vector index)."""
    vec = [1.0] + [0.0] * 1023
    rig = Rig(driver, FakeJudge())
    await rig.q(
        "CREATE (:Event:Document {event_id: 'pdf-1', event_time: datetime('2019-11-12T09:00:00Z'), "
        "platform: 'pdf', test_capture: true})"
    )
    note = await rig.repo.assert_(
        AssertionIn(
            statement="Gen1 units: a port bounce cleared the forwarding issue in 4/4 cases.",
            topic="upgrade_notes",  # neither the draft's topic nor its entities
            bindings=[{"entity_id": "TOPIC:upgrade_sfp_400g", "scope": "activity"}],
            sources=[{"event_id": "pdf-1", "quote": "port bounce cleared"}],
        )
    )
    await rig.repo.set_embeddings([{"id": note.assertion_id, "vector": vec}])
    await rig.q("CALL db.awaitIndexes(60)")

    async def embed(text: str) -> list[float] | None:
        return vec

    rig.loop.extractor.embed = embed
    rig.judge.relations = {note.assertion_id: "corroborates"}
    aid = await _capture_elisa(rig)
    (a,) = await rig.repo.assertions([aid])
    assert a.corroborates == [note.assertion_id]
