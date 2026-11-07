"""Ops agent rules, Slack rendering, entity resolution and telemetry checks (no services)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from kaiku.agents.ops_agent import (
    AnswerStatus,
    ConditionReport,
    OpsAnswer,
    Source,
    Step,
    _from_plain_text,
    enforce_rules,
    name_successors,
    seen_assertion_ids,
    seen_records,
)
from kaiku.context.resolve import resolve
from kaiku.seed.sources import MockData
from kaiku.slack.format import SLACK_TEXT_LIMIT, format_answer
from kaiku.telemetry.store import ConditionStatus, TelemetryStore

MOCK = Path(__file__).parents[2] / "mock_data"


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- Agent hard rules --------------------------------------------------------------------------


def _answer(**kw: object) -> OpsAnswer:
    base: dict[str, object] = {"status": AnswerStatus.ANSWERED, "summary": "s"}
    return OpsAnswer.model_validate({**base, **kw})


def test_uncited_and_invented_citations_are_removed() -> None:
    a = _answer(
        steps=[
            Step(action="bounce", assertion_ids=["ASR-0001"]),
            Step(action="made up", assertion_ids=["ASR-9999"]),
            Step(action="no source", assertion_ids=[]),
        ],
        sources=[Source(assertion_id="ASR-0001"), Source(assertion_id="ASR-9999")],
    )
    out = enforce_rules(a, seen={"ASR-0001"})
    assert [s.action for s in out.steps] == ["bounce"]
    assert [s.assertion_id for s in out.sources] == ["ASR-0001"]
    assert len(out.review_notes) == 2


def test_step_with_unmet_conditions_is_removed() -> None:
    a = _answer(
        steps=[Step(action="bounce", assertion_ids=["ASR-0001"])],
        conditions=[
            ConditionReport(assertion_id="ASR-0001", name="counters_zero", status="not_met")
        ],
        sources=[Source(assertion_id="ASR-0001")],
    )
    out = enforce_rules(a, seen={"ASR-0001"})
    assert out.steps == []
    assert "not met" in out.review_notes[0]


def test_unknown_conditions_keep_the_step() -> None:
    a = _answer(
        steps=[Step(action="bounce", assertion_ids=["ASR-0001"])],
        conditions=[
            ConditionReport(assertion_id="ASR-0001", name="counters_zero", status="unknown")
        ],
    )
    assert len(enforce_rules(a, seen={"ASR-0001"}).steps) == 1


def test_not_documented_has_no_steps_and_unsupported_answer_becomes_not_documented() -> None:
    a = _answer(
        status=AnswerStatus.NOT_DOCUMENTED, steps=[Step(action="x", assertion_ids=["ASR-0001"])]
    )
    assert enforce_rules(a, seen={"ASR-0001"}).steps == []
    b = _answer(steps=[Step(action="guess", assertion_ids=["ASR-0042"])])
    assert enforce_rules(b, seen=set()).status is AnswerStatus.NOT_DOCUMENTED


def test_a_network_fact_stands_on_a_record_get_entity_returned() -> None:
    a = _answer(records=["GEN-1"])
    out = enforce_rules(a, seen=set(), records={"GEN-1"})
    assert out.status is AnswerStatus.ANSWERED
    assert out.records == ["GEN-1"]
    # A record the tools never returned is dropped, and the answer with it.
    out = enforce_rules(a, seen=set(), records=set())
    assert (out.status, out.records) == (AnswerStatus.NOT_DOCUMENTED, [])
    # A step still needs an assertion, record or not.
    b = _answer(records=["GEN-1"], steps=[Step(action="reboot", assertion_ids=["ASR-0042"])])
    assert enforce_rules(b, seen=set(), records={"GEN-1"}).steps == []


def test_records_come_from_get_entity_results_only() -> None:
    messages = [
        ToolMessage(
            content=json.dumps({"entity_id": "TRN-042"}), name="get_entity", tool_call_id="1"
        ),
        ToolMessage(
            content=json.dumps({"entity_id": "TRN-001"}), name="resolve_entities", tool_call_id="2"
        ),
    ]
    assert seen_records(messages) == {"TRN-042"}


def test_seen_ids_come_from_tool_results_only() -> None:
    payload = json.dumps({"assertions": [{"assertion_id": "ASR-0001", "supersedes": "ASR-0030"}]})
    messages = [
        HumanMessage(content="what about ASR-0777?"),
        AIMessage(content="ASR-0888"),
        ToolMessage(content=[{"type": "text", "text": payload}], tool_call_id="1"),
        ToolMessage(content="see ASR-0024", tool_call_id="2"),
    ]
    assert seen_assertion_ids(messages) == {"ASR-0001", "ASR-0030", "ASR-0024"}


def test_superseded_source_names_its_successor() -> None:
    """ST-73 (plan 004 R11): the padlock cited for a past visit names the code lock."""
    payload = json.dumps(
        {
            "assertions": [
                {
                    "assertion_id": "ASR-0023",
                    "status": "superseded",
                    "superseded_by": "ASR-0024",
                    "valid_to": "2026-08-02T21:00:00Z",
                },
                {"assertion_id": "ASR-0025", "superseded_by": None},
            ]
        }
    )
    messages = [ToolMessage(content=[{"type": "text", "text": payload}], tool_call_id="1")]
    a = _answer(sources=[Source(assertion_id="ASR-0023"), Source(assertion_id="ASR-0025")])
    out = name_successors(a, messages)
    assert out.review_notes == [
        "ASR-0023 was superseded by ASR-0024 from 2026-08-03 00:00 Helsinki time."
    ]
    assert name_successors(out, messages).review_notes == out.review_notes  # idempotent


def test_plain_text_ending_becomes_a_clarification() -> None:
    out = _from_plain_text(
        [HumanMessage(content="q"), AIMessage(content="Which site do you mean?")]
    )
    assert out.status is AnswerStatus.NEEDS_CLARIFICATION
    assert out.summary == "Which site do you mean?"
    assert out.steps == []
    with pytest.raises(RuntimeError):
        _from_plain_text([HumanMessage(content="q")])


def test_slack_rendering() -> None:
    a = _answer(
        steps=[Step(action="Disable and re-enable 1/1/2", assertion_ids=["ASR-0001"])],
        avoid=["replace_sfp", "reboot_node"],
        conditions=[
            ConditionReport(assertion_id="ASR-0001", name="oper_up", status="met", detail="UP"),
            ConditionReport(assertion_id="ASR-0001", name="counters_zero", status="unknown"),
        ],
        sources=[
            Source(
                assertion_id="ASR-0001",
                who="erkki",
                where="teams_message",
                when="2025-10-14T06:22:08Z",
            )
        ],
        gap_id="GAP-1",
    )
    text = format_answer(a)
    assert "1. Disable and re-enable 1/1/2  _(ASR-0001)_" in text
    assert "*Avoid:* replace sfp; reboot node" in text
    assert ":white_check_mark: oper up — UP" in text
    assert ":grey_question: counters zero" in text
    assert "ASR-0001: erkki, teams message, 2025-10-14" in text
    assert "GAP-1" in text
    long = format_answer(_answer(summary="x" * 5000))
    assert len(long) == SLACK_TEXT_LIMIT


# --- Entity resolution -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def entities() -> tuple[list[dict[str, object]], dict[str, str]]:
    data = MockData(MOCK)
    rows: list[dict[str, object]] = [
        {
            "entity_id": e.entity_id,
            "entity_type": e.type,
            "name": e.name,
            "city": e.city,
            "private_road": e.private_road,
            "valid_from": e.valid_from,
            "valid_to": e.valid_to,
        }
        for e in data.entities()
    ]
    rows += [
        {"entity_id": g, "entity_type": "generation", "valid_from": utc("2018-01-01T00:00:00Z")}
        for g in ("Gen1", "Gen2", "Gen3")
    ]
    return rows, data.model_aliases()


@pytest.mark.parametrize(
    ("text", "best"),
    [
        ("I swapped the SFP on the Pori node and no traffic", "TRN-017"),
        ("how do I get through the gate at the Kemi site?", "SEG-042"),
        ("the older transmission units", "TNC-1X"),
        ("which firmware fixes the bug on the old transmission units?", "TNC-1X"),  # fuzzy
        ("trn-042 port 1/1/3", "TRN-042"),
        ("anything about the Gen2 boxes?", "Gen2"),
    ],
)
def test_resolve(
    entities: tuple[list[dict[str, object]], dict[str, str]], text: str, best: str
) -> None:
    rows, aliases = entities
    found = resolve(text, rows, aliases, datetime(2026, 11, 7, tzinfo=UTC))
    assert found
    assert found[0].entity_id == best


def test_resolve_skips_decommissioned_devices(
    entities: tuple[list[dict[str, object]], dict[str, str]],
) -> None:
    rows, aliases = entities
    # TRN-004 was decommissioned on 2026-05-11.
    assert resolve("TRN-004", rows, aliases, utc("2026-01-01T00:00:00Z"))
    assert resolve("TRN-004", rows, aliases, utc("2026-06-01T00:00:00Z")) == []


def test_resolve_nothing(entities: tuple[list[dict[str, object]], dict[str, str]]) -> None:
    rows, aliases = entities
    assert resolve("hello there", rows, aliases, datetime(2026, 11, 7, tzinfo=UTC)) == []


# --- Telemetry conditions ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def telemetry() -> TelemetryStore:
    return TelemetryStore(MOCK)


SYMPTOMS = ["oper_up", "rx_power_normal", "counters_zero"]


def test_elisa_moment_all_conditions_met(telemetry: TelemetryStore) -> None:
    # Message 7, "Counters basically zero", at 06:21:30Z: checked against 06:00-06:15.
    results = telemetry.check("TRN-042", "1/1/3", SYMPTOMS, utc("2025-10-14T06:21:30Z"))
    assert [r.status for r in results] == [ConditionStatus.MET] * 3
    assert results[0].window_start == utc("2025-10-14T06:00:00Z")


def test_port_down_during_the_swap_is_not_traffic_zero(telemetry: TelemetryStore) -> None:
    r = telemetry.check("TRN-042", "1/1/3", ["counters_zero"], utc("2025-10-14T06:00:00Z"))[0]
    assert r.status is ConditionStatus.NOT_MET
    assert "DOWN" in r.detail


def test_after_recovery_traffic_is_flowing(telemetry: TelemetryStore) -> None:
    r = telemetry.check("TRN-042", "1/1/3", ["counters_zero"], utc("2025-10-14T07:00:00Z"))[0]
    assert r.status is ConditionStatus.NOT_MET
    assert "flowing" in r.detail


def test_no_telemetry_is_unknown_never_met(telemetry: TelemetryStore) -> None:
    results = telemetry.check(
        "TRN-017", "1/1/3", [*SYMPTOMS, "alarm_cleared"], utc("2026-11-07T09:00:00Z")
    )
    assert {r.status for r in results} == {ConditionStatus.UNKNOWN}


async def test_three_tool_errors_in_a_row_fail_the_run() -> None:
    from types import SimpleNamespace

    from kaiku.agents.ops_agent import ToolErrorLimit, ToolErrorsExceeded

    guard = ToolErrorLimit()
    request = SimpleNamespace(tool_call={"name": "get_context"})

    def reply(status: str) -> object:
        async def handler(_: object) -> ToolMessage:
            return ToolMessage("x", tool_call_id="1", status=status)  # type: ignore[arg-type]

        return handler

    for status in ["error", "error", "success", "error", "error"]:  # a success resets
        await guard.awrap_tool_call(request, reply(status))
    with pytest.raises(ToolErrorsExceeded, match="3 tool errors in a row"):
        await guard.awrap_tool_call(request, reply("error"))
