"""SL-2: the proactive fault thread, outcome buttons and "ask the expert" (no Slack needed)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import ToolMessage

from kaiku.agents.ops_agent import AnswerStatus, ConditionReport, OpsAnswer, Step, opened_gap
from kaiku.slack import actions as a

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
FAULT = [
    {
        "event_id": f"SIM-{i}",
        "ne_id": "TRN-017",
        "port": "1/1/2",
        "type": t,
        "event_time": "2026-11-07T10:45:00+00:00",
        "kind": "Alarm",
    }
    for i, t in enumerate(["MODULE_INSERTED", "LINK_UP", "OPTICAL_POWER_OK"])
]
ADVICE = OpsAnswer(
    status=AnswerStatus.ANSWERED,
    summary="Silent stall after an SFP swap: bounce the port.",
    steps=[Step(action="Disable and re-enable 1/1/2", assertion_ids=["ASR-0001"])],
    avoid=["replace_sfp", "reboot_node"],
    conditions=[ConditionReport(assertion_id="ASR-0001", name="counters_zero", status="met")],
)
UNKNOWN = OpsAnswer(
    status=AnswerStatus.NOT_DOCUMENTED,
    summary="I found no record of this.",
    gap_id="GAP-1",
    gap_routed_to="erkki",
)


class Client:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.channels: list[dict[str, Any]] = []
        self.joined: list[str] = []

    async def chat_postMessage(self, **kw: Any) -> dict[str, Any]:
        self.posted.append(kw)
        return {"ts": f"9.{len(self.posted):06d}"}

    async def chat_update(self, **kw: Any) -> dict[str, Any]:
        self.updated.append(kw)
        return {"ok": True}

    async def chat_getPermalink(self, **kw: Any) -> dict[str, Any]:
        return {"permalink": f"https://x.slack.com/{kw['channel']}/{kw['message_ts']}"}

    async def conversations_list(self, **kw: Any) -> dict[str, Any]:
        return {"channels": self.channels}

    async def conversations_join(self, **kw: Any) -> dict[str, Any]:
        self.joined.append(kw["channel"])
        return {"ok": True}

    async def users_list(self, **kw: Any) -> dict[str, Any]:
        return {
            "members": [
                {"id": "UBOT", "name": "kaiku", "is_bot": True},
                {"id": "U9", "name": "erkki.v", "profile": {"real_name": "Erkki Virtanen"}},
            ]
        }


class Answerer:
    def __init__(self, answer: OpsAnswer | Exception = ADVICE) -> None:
        self.answer, self.asked = answer, []

    async def ask(
        self,
        question: str,
        *,
        request_id: str,
        asker: str | None = None,
        trigger_event_id: str | None = None,
    ) -> OpsAnswer:
        self.asked.append((question, request_id))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Tools:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if self.fail:
            raise RuntimeError("report_outcome failed: unknown entity")
        return {"support_n": 1, "refute_n": 0}


class Network:
    def __init__(self, flowing: bool | None = True) -> None:
        self.feed: list[dict[str, Any]] = []
        self.bounced: list[tuple[str, str]] = []
        self.flowing = flowing

    async def events(self) -> list[dict[str, Any]]:
        return list(self.feed)

    async def bounce(self, node: str, port: str) -> None:
        self.bounced.append((node, port))

    async def traffic_flowing(self, node: str, port: str) -> bool | None:
        return self.flowing


def _ops(**kw: Any) -> a.OpsActions:
    defaults: dict[str, Any] = {
        "network": Network(),
        "alarm_channel": "CALARM",
        "settle": timedelta(0),
        "clock": lambda: NOW,
    }
    answerer = kw.pop("answerer", Answerer())
    tools = kw.pop("tools", Tools())
    return a.OpsActions(answerer, tools, **(defaults | kw))


def _action_ids(blocks: list[dict[str, Any]]) -> list[str]:
    return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]


def _click(message: dict[str, Any], action_id: str) -> dict[str, Any]:
    blocks = message["blocks"]
    [value] = [
        e["value"]
        for b in blocks
        if b["type"] == "actions"
        for e in b["elements"]
        if e["action_id"] == action_id
    ]
    return {
        "actions": [{"action_id": action_id, "value": value}],
        "channel": {"id": message["channel"]},
        "message": {"ts": message["ts"], "blocks": blocks},
        "user": {"id": "U5"},
    }


async def _ack() -> None:
    return None


def test_buttons_fit_the_answer() -> None:
    assert _action_ids(a.answer_blocks(ADVICE)) == []  # asked, no faulty port: no outcome
    fault = a.answer_blocks(ADVICE, fault=FAULT[2])
    assert _action_ids(fault) == [a.RUN_BOUNCE, a.DONE_MANUALLY, a.DIDNT_WORK]
    value = json.loads(fault[-1]["elements"][0]["value"])
    assert value == {
        "node": "TRN-017",
        "port": "1/1/2",
        "assertion_id": "ASR-0001",
        "trigger": "SIM-2",
    }
    assert _action_ids(a.answer_blocks(UNKNOWN, question="q")) == [a.GAP_ASK, a.GAP_DONT_ASK]
    # Not documented never offers outcome buttons: there is no advice to confirm.
    assert a.RUN_BOUNCE not in _action_ids(a.answer_blocks(UNKNOWN, fault=FAULT[2]))


async def test_a_new_fault_posts_one_alarm_with_advice_in_its_thread() -> None:
    client, answerer = Client(), Answerer()
    ops = _ops(answerer=answerer)
    ops.network.feed = [{**FAULT[2], "event_id": "OLD"}]  # type: ignore[union-attr]
    assert await ops.poll_once(client) == 0  # first poll: what already happened isn't re-posted
    ops.network.feed += FAULT  # type: ignore[union-attr]
    assert await ops.poll_once(client) == 1
    assert await ops.poll_once(client) == 0  # once only

    head, reply = client.posted
    assert head["channel"] == "CALARM"
    assert "TRN-017 1/1/2" in head["text"]
    assert reply["thread_ts"] == "9.000001"
    [question] = [q for q, _ in answerer.asked]
    assert "TRN-017 port 1/1/2" in question
    [update] = client.updated
    assert update["ts"] == "9.000002"  # the thread reply, edited in place
    assert "*Avoid:* replace sfp; reboot node" in update["text"]
    assert _action_ids(update["blocks"]) == [a.RUN_BOUNCE, a.DONE_MANUALLY, a.DIDNT_WORK]


async def test_agent_failure_still_answers_the_alarm() -> None:
    client = Client()
    ops = _ops(answerer=Answerer(TimeoutError("slow")))
    await ops.post_fault(client, FAULT[2])  # type: ignore[arg-type]
    assert "couldn't check this fault (TimeoutError)" in client.updated[0]["text"]


async def _advice_message(ops: a.OpsActions) -> dict[str, Any]:
    client = Client()
    await ops.post_fault(client, FAULT[2])  # type: ignore[arg-type]
    return {**client.updated[0], "ts": "9.000002"}


async def test_run_bounce_reports_a_network_confirmed_outcome_through_mcp() -> None:
    tools, net = Tools(), Network(flowing=True)
    ops = _ops(tools=tools, network=net)
    message = await _advice_message(ops)
    client = Client()
    await ops.on_outcome(_ack, _click(message, a.RUN_BOUNCE), client)  # type: ignore[arg-type]

    assert net.bounced == [("TRN-017", "1/1/2")]
    [(name, args)] = tools.calls
    assert name == "report_outcome"
    assert args | {"evidence_ref": None} == {
        "entity_id": "TRN-017",
        "port": "1/1/2",
        "action": "bounce_interface",
        "result": "resolved",
        "assertion_id": "ASR-0001",
        "observed_at": NOW.isoformat(),  # when it was done: conditions are judged before it
        "evidence_ref": None,
        "on_behalf_of": "U5",
        "trigger_event_id": "SIM-2",  # the fault the advice answered
        "idempotency_key": "slack:CALARM:9.000002",  # a double click replays
    }
    progress, final = client.updated
    assert "Bouncing TRN-017 1/1/2 for <@U5>" in progress["text"]
    assert "network-confirmed on TRN-017 1/1/2 · support 1" in final["text"]
    assert _action_ids(final["blocks"]) == []  # buttons gone, the note stays
    assert "Silent stall" in final["blocks"][0]["text"]["text"]  # the advice stays


async def test_counters_still_zero_after_the_bounce_is_not_resolved() -> None:
    tools = Tools()
    ops = _ops(tools=tools, network=Network(flowing=False))
    client = Client()
    await ops.on_outcome(_ack, _click(await _advice_message(ops), a.RUN_BOUNCE), client)  # type: ignore[arg-type]
    assert tools.calls[0][1]["result"] == "not_resolved"
    assert "counters still at zero" in client.updated[-1]["text"]


async def test_manual_buttons_report_without_touching_the_network() -> None:
    for action_id, result in ((a.DONE_MANUALLY, "resolved"), (a.DIDNT_WORK, "not_resolved")):
        tools, net = Tools(), Network()
        ops = _ops(tools=tools, network=net)
        client = Client()
        await ops.on_outcome(_ack, _click(await _advice_message(ops), action_id), client)  # type: ignore[arg-type]
        assert net.bounced == []
        assert tools.calls[0][1]["result"] == result
        assert "<@U5>" in client.updated[-1]["text"]


async def test_a_failed_report_is_shown_not_swallowed() -> None:
    ops = _ops(tools=Tools(fail=True))
    client = Client()
    await ops.on_outcome(_ack, _click(await _advice_message(ops), a.DIDNT_WORK), client)  # type: ignore[arg-type]
    assert "Couldn't record the outcome (RuntimeError)" in client.updated[-1]["text"]


def _gap_message() -> dict[str, Any]:
    return {
        "channel": "CASK",
        "ts": "5.000001",
        "blocks": a.answer_blocks(UNKNOWN, question="Kajaani boom gate code?"),
    }


async def test_ask_posts_the_question_to_the_routed_expert() -> None:
    client = Client()
    ops = _ops(gap_channel="CTRANSPORT")
    await ops.on_gap(_ack, _click(_gap_message(), a.GAP_ASK), client)  # type: ignore[arg-type]
    [ask] = client.posted
    assert ask["channel"] == "CTRANSPORT"
    assert ask["text"].startswith(":question: <@U9>, <@U5> asked")
    assert "Kajaani boom gate code?" in ask["text"]
    assert "GAP-1" in ask["text"]
    [update] = client.updated
    assert "Asked <@U9>" in update["text"]
    assert "CTRANSPORT/9.000001" in update["text"]
    assert _action_ids(update["blocks"]) == []


async def test_dont_ask_posts_nothing() -> None:
    client = Client()
    await _ops().on_gap(_ack, _click(_gap_message(), a.GAP_DONT_ASK), client)  # type: ignore[arg-type]
    assert client.posted == []
    assert "Not asked" in client.updated[0]["text"]


def test_gap_routing_comes_from_the_tool_result_not_the_model() -> None:
    result = {"gap_id": "GAP-7", "routed_to": "erkki", "question": "q", "entity_ids": ["X"]}
    msgs = [
        ToolMessage(content="[]", tool_call_id="1", name="get_context"),
        ToolMessage(content=json.dumps(result), tool_call_id="2", name="open_knowledge_gap"),
    ]
    assert opened_gap(msgs) == {"gap_id": "GAP-7", "routed_to": "erkki"}
    assert opened_gap(msgs[:1]) is None


def test_slack_sources_link_to_the_message() -> None:
    from kaiku.agents.ops_agent import Source
    from kaiku.slack.format import format_answer

    answer = ADVICE.model_copy(
        update={
            "sources": [
                Source(
                    assertion_id="ASR-1",
                    who="erkki",
                    where="#noc-transport",
                    link="https://x.slack.com/archives/C1/p1",
                ),
                Source(assertion_id="ASR-2", who="pekka", link="javascript:alert(1)"),
            ]
        }
    )
    text = format_answer(answer)
    assert "• <https://x.slack.com/archives/C1/p1|ASR-1>: erkki, #noc-transport" in text
    assert "• ASR-2: pekka" in text  # only https links are rendered


async def test_the_network_is_watched_even_without_an_alarm_channel_setting() -> None:
    # Settings only choose where to post. No SLACK_ALARM_CHANNEL: the #noc-alarms channel
    # (joined if needed), else the first capture channel, else the fault is still seen and logged.
    client = Client()
    client.channels = [{"id": "CGEN", "name": "general"}, {"id": "CNOC", "name": "noc-alarms"}]
    ops = _ops(alarm_channel=None)
    await ops.post_fault(client, FAULT[2])  # type: ignore[arg-type]
    assert client.posted[0]["channel"] == "CNOC"
    assert client.joined == ["CNOC"]

    client = Client()
    ops = _ops(alarm_channel=None, capture_channels=["CCAP", "CCAP2"])
    await ops.post_fault(client, FAULT[2])  # type: ignore[arg-type]
    assert client.posted[0]["channel"] == "CCAP"

    client, answerer = Client(), Answerer()
    ops = _ops(alarm_channel=None, answerer=answerer)
    await ops.post_fault(client, FAULT[2])  # type: ignore[arg-type]
    assert client.posted == []  # nowhere to post: logged, and no LLM call wasted
    assert answerer.asked == []


async def test_with_no_known_expert_the_channel_is_asked() -> None:
    message = {
        "channel": "CASK",
        "ts": "5.000001",
        "blocks": a.answer_blocks(
            UNKNOWN.model_copy(update={"gap_routed_to": None}), question="Kajaani gate code?"
        ),
    }
    client = Client()
    await _ops().on_gap(_ack, _click(message, a.GAP_ASK), client)  # type: ignore[arg-type]
    [ask] = client.posted
    assert ask["channel"] == "CASK"  # no SLACK_GAP_CHANNEL: where it was asked
    assert ask["text"].startswith(":question: Does anyone know? <@U5> asked something")
    assert "Anyone" not in ask["text"]
    assert client.updated[0]["text"].startswith("Asked in <#CASK> (")


def test_sources_are_grounded_in_the_tool_citations() -> None:
    from kaiku.agents.ops_agent import Source, ground_sources
    from kaiku.slack.format import format_answer

    context = {
        "assertions": [
            {
                "assertion_id": "ASR-0001",
                "citations": [
                    {
                        "source_type": "teams_message",
                        "source_ref": "7826958213559",
                        "detail": "NOC-Transport thread, message 8/12",
                        "author": "erkki",
                        "recorded_at": "2025-10-14T06:22:08Z",
                    },
                    {
                        "source_type": "slack",
                        "source_ref": "https://x.slack.com/archives/C1/p17",
                        "detail": "Disable and re-enable the interface before swapping hardware",
                        "author": "erkki",
                        "recorded_at": "2026-11-07T10:01:00Z",
                    },
                ],
            },
            {
                "assertion_id": "ASR-0024",
                "citations": [
                    {
                        "source_type": "email",
                        "source_ref": "<BDB4@volga.example>",
                        "detail": "Kemi site: new code lock installed",
                        "author": "timo",
                        "recorded_at": "2026-08-18T09:00:00Z",
                    }
                ],
            },
        ]
    }
    msgs = [ToolMessage(content=json.dumps(context), tool_call_id="1", name="get_context")]
    model_said = ADVICE.model_copy(
        update={
            "sources": [
                Source(assertion_id="ASR-0001", who="Erkki?", where="NOC-Transport thread"),
                Source(assertion_id="ASR-0024", who="timo", where="email"),
                Source(assertion_id="ASR-9999", who="nobody"),  # not in the tool output
            ]
        }
    )
    grounded = ground_sources(model_said, msgs)
    s1, s2, s3 = grounded.sources
    # The captured Slack message wins over the backfilled reference: a real permalink.
    assert (s1.who, s1.link, s1.ref, s1.when) == (
        "erkki",
        "https://x.slack.com/archives/C1/p17",
        None,
        "2026-11-07",
    )
    assert (s2.ref, s2.link, s2.detail) == (
        "<BDB4@volga.example>",
        None,
        "Kemi site: new code lock installed",
    )
    assert s3 == model_said.sources[2]  # nothing to ground it in: unchanged

    text = format_answer(grounded)
    assert "• <https://x.slack.com/archives/C1/p17|ASR-0001>: erkki, slack, 2026-11-07" in text
    assert "    ↳ _Disable and re-enable the interface before swapping hardware_" in text
    assert "• ASR-0024: timo, email, 2026-08-18 · `&lt;BDB4@volga.example&gt;`" in text
