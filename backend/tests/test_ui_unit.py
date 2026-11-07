"""The console API (UI-1): gates read back from a capture, the live-stream diff, agent-run
titles, and the MCP wrapper that records tool calls. No Neo4j, no LLM."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from kaiku.context.models import AssertionView, Binding, BindingIn, Scope, SourceIn
from kaiku.domain.models import AssertionStatus
from kaiku.extraction.gates import Grounded
from kaiku.extraction.graph import CaptureResult, Item, Step
from kaiku.extraction.link import Linked
from kaiku.extraction.models import Mention, Quoted, RuleDraft
from kaiku.extraction.reconcile import Decision
from kaiku.mcp.server import ServiceHolder, create_mcp_server
from kaiku.ui import gates
from kaiku.ui.models import ToolCallView
from kaiku.ui.router import _title, diff

T = datetime(2025, 10, 14, 6, 22, 8, tzinfo=UTC)


def _draft() -> RuleDraft:
    return RuleDraft(
        statement="On Gen1 units, bounce the port when counters stay at zero after an SFP swap.",
        topic="forwarding_state_after_sfp_swap",
        action=Quoted(value="bounce_interface", quote="Disable the interface ... enable it again"),
        conditions=[Quoted(value="counters_zero", quote="Counters basically zero")],
        avoid=[Quoted(value="reboot_node", quote="Don't reboot the whole unit")],
        entities=[Mention(surface_form="TRN-042", role=Scope.OBSERVED_ON)],
    )


def _item(**update: Any) -> Item:
    draft = _draft()
    base = Item(
        draft=draft,
        grounded=Grounded(
            draft=draft,
            sources=[SourceIn(event_id="m8", quote="Disable the interface")],
            recorded_at=T,
            valid_from=T,
            observed_at=T,
        ),
        linked=Linked(
            bindings=[
                BindingIn(entity_id="TRN-042", scope=Scope.OBSERVED_ON),
                BindingIn(entity_id="TNC-1X", scope=Scope.APPLIES_TO),
            ]
        ),
        telemetry="agrees",
        telemetry_entity="TRN-042",
        telemetry_port="1/1/3",
        telemetry_detail="counters_zero: 0 pps",
        decision=Decision(supersede="ASR-0030", corroborates=["ASR-0002"]),
        assertion_id="ASR-NEW",
    )
    return base.model_copy(update=update)


def _results(item: Item) -> dict[str, str]:
    view = gates.items(
        CaptureResult(thread_id="t", status="captured", kind="rule", reason="r", items=[item])
    )[0]
    return {g.code: g.result for g in view.gates}


def test_every_gate_passes_for_a_clean_capture() -> None:
    assert _results(_item()) == {
        "V1-V2": "pass",
        "V3": "pass",
        "V4": "pass",
        "V5": "pass",
        "V6": "info",
    }


def test_v6_names_what_the_rule_replaced_and_agrees_with() -> None:
    view = gates.items(
        CaptureResult(thread_id="t", status="captured", kind="rule", reason="r", items=[_item()])
    )[0]
    v6 = next(g for g in view.gates if g.code == "V6")
    assert v6.detail == "supersedes ASR-0030; corroborates ASR-0002"
    assert view.quotes["action"] == ["Disable the interface ... enable it again"]
    assert view.quotes["entity"] == ["TRN-042"]


def test_a_rule_dropped_by_grounding_skips_the_later_gates() -> None:
    item = Item(draft=_draft(), dropped="V2: the action comes from a question, not an answer")
    assert _results(item) == {
        "V1-V2": "fail",
        "V3": "skip",
        "V4": "skip",
        "V5": "skip",
        "V6": "skip",
    }


def test_contradiction_and_telemetry_disagreement_are_flagged() -> None:
    item = _item(telemetry="disagrees", decision=Decision(contradicts=["ASR-0020"]))
    r = _results(item)
    assert r["V4"] == "flag"
    assert r["V6"] == "flag"


def test_an_unconfirmed_entity_is_flagged_with_the_question() -> None:
    item = _item(
        linked=Linked(
            bindings=[BindingIn(entity_id="Gen1", scope=Scope.APPLIES_TO, candidate=True)],
            question="Which one is the older unit?",
        )
    )
    view = gates.items(
        CaptureResult(thread_id="t", status="captured", kind="rule", reason="r", items=[item])
    )[0]
    v3 = next(g for g in view.gates if g.code == "V3")
    assert v3.result == "flag"
    assert "Which one is the older unit?" in v3.detail


def test_a_rejected_write_fails_v5() -> None:
    assert _results(_item(assertion_id=None, dropped="write rejected: R4"))["V5"] == "fail"


def test_steps_are_summed_per_node_in_pipeline_order() -> None:
    raw = [
        Step(node="triage", ms=300),
        Step(node="extract", ms=2000),
        Step(node="link", ms=10),
        Step(node="validate", ms=40),
        Step(node="reconcile", ms=100),
        Step(node="persist", ms=50),
        Step(node="reconcile", ms=120),
        Step(node="persist", ms=60),
    ]
    out = [(s.node, s.ms, s.runs) for s in gates.steps(raw)]
    assert out == [
        ("triage", 300, 1),
        ("extract", 2000, 1),
        ("link", 10, 1),
        ("validate", 40, 1),
        ("reconcile", 220, 2),
        ("persist", 110, 2),
    ]


def test_a_capture_from_before_tracing_shows_what_the_assertion_kept() -> None:
    view = AssertionView(
        assertion_id="ASR-1",
        statement="Call the Ivalo fire station below -20 C.",
        topic="access_keys",
        status=AssertionStatus.UNVERIFIED,
        valid_from=T,
        recorded_at=T,
        qualifiers={
            "quotes": {
                "action": "call the Ivalo fire station",
                "conditions": ["below -20 °C"],
                "avoid": [],
            },
            "telemetry": "unknown",
        },
        bindings=[Binding(entity_id="TRN-011", scope=Scope.OBSERVED_ON)],
        corroborates=["ASR-0025"],
    )
    item = gates.stored(view)
    assert item.quotes == {"condition": ["below -20 °C"], "action": ["call the Ivalo fire station"]}
    assert [g.code for g in item.gates] == ["V1-V2", "V3", "V6"]


def test_capture_result_round_trips_through_json() -> None:
    """The console reads what the capture loop stored on the Thread."""
    r = CaptureResult(
        thread_id="t",
        status="captured",
        kind="rule",
        reason="r",
        items=[_item()],
        started_at=T,
        steps=[Step(node="triage", ms=1)],
        trace_id="ab" * 16,
    )
    assert CaptureResult.model_validate_json(r.model_dump_json()) == r


def _snap(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "assertions": {"A": "believed_true||0/0"},
        "threads": {"t1": "captured|x|y"},
        "relations": 1,
        "calls": 0,
    }
    return {**base, **over}


def test_diff_is_none_when_nothing_changed() -> None:
    assert diff(_snap(), _snap()) is None


def test_diff_names_new_assertions_and_changed_threads() -> None:
    cur = _snap(
        assertions={"A": "believed_true||0/0", "B": "unverified||0/0"},
        threads={"t1": "captured|x|z"},
    )
    c = diff(_snap(), cur)
    assert c is not None
    assert c.new_assertions == ["B"]
    assert c.new_captures == ["t1"]
    assert c.assertions
    assert c.captures
    assert not c.agents


def test_diff_sees_a_status_change_and_a_tool_call() -> None:
    c = diff(_snap(), _snap(assertions={"A": "disputed||0/1"}, calls=3))
    assert c is not None
    assert c.assertions
    assert c.agents
    assert c.new_assertions == []


def _call(tool: str, **args: Any) -> ToolCallView:
    return ToolCallView(
        call_id=tool, agent_id="ops-agent", tool=tool, args=args, ok=True, summary="", ms=1, at=T
    )


def test_run_title_prefers_the_question_then_the_entity() -> None:
    assert (
        _title(
            [
                _call("get_entity", entity_id="TRN-017"),
                _call("resolve_entities", text="the Pori node"),
            ]
        )
        == "the Pori node"
    )
    assert _title([_call("get_context", entity_id="TRN-017")]) == "get context · TRN-017"


@pytest.mark.asyncio
async def test_mcp_tools_keep_their_schema_and_record_each_call() -> None:
    holder = ServiceHolder(stdio_agent_id="tester")
    recorded: list[dict[str, Any]] = []
    holder.record = recorded.append  # type: ignore[method-assign,assignment]
    server = create_mcp_server({"tester": SecretStr("x" * 32)}, holder)

    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools["get_context"].inputSchema["properties"]) >= {"entity_id", "as_of", "known_at"}
    assert tools["get_context"].inputSchema["required"] == ["entity_id"]

    # The service is not up yet: the call fails, and the failure is still recorded.
    with pytest.raises(Exception, match="starting"):
        await server.call_tool("get_context", {"entity_id": "TRN-017"})
    assert len(recorded) == 1
    call = recorded[0]
    assert call["tool"] == "get_context"
    assert call["agent_id"] == "tester"
    assert call["ok"] is False
    assert '"entity_id": "TRN-017"' in call["args_json"]
    # ping is not an agent's work: not recorded.
    await server.call_tool("ping", {})
    assert len(recorded) == 1


@pytest.mark.asyncio
async def test_people_only_writes_are_not_mcp_tools() -> None:
    """Confirm, retract, correct and edit validity are the Slack card's, never an agent's
    (plan 001 §7, plan 002 §4.2): the MCP tool list has the four agent writes only."""
    server = create_mcp_server({"tester": SecretStr("x" * 32)}, ServiceHolder())
    names = {t.name for t in await server.list_tools()}
    assert names >= {"report_outcome", "open_knowledge_gap", "propose_assertion", "flag_assertion"}
    people_only = {"confirm", "retract", "correct", "supersede", "assert", "set", "edit", "delete"}
    assert [n for n in names if n.split("_")[0] in people_only] == []
    assert not [n for n in names if "valid" in n]  # no tool sets validity
