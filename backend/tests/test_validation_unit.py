"""The outcome verifier's judgement, confidence, and how the card shows them (plan 001 §6.2).
Against Neo4j: tests/test_integration_outcomes.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kaiku.context.models import (
    AssertionView,
    Binding,
    ContextResult,
    OutcomeRecorded,
    OutcomeVerification,
    Scope,
)
from kaiku.domain.models import AssertionStatus
from kaiku.domain.time import PM_INTERVAL
from kaiku.ingest.store import ThreadView
from kaiku.mcp.server import context_markdown, summarize
from kaiku.slack import actions as a
from kaiku.slack import capture as cap
from kaiku.telemetry.store import ConditionResult, ConditionStatus
from kaiku.validation.outcomes import (
    VERIFY_WINDOW,
    Observation,
    beta_confidence,
    conditions_met,
    evidence_line,
    judge,
)

T = datetime(2026, 11, 7, 10, 5, tzinfo=UTC)  # off the 15-minute grid on purpose


def _bin(start: datetime, mbps: float, status: str = "UP") -> Observation:
    return Observation(start, start + PM_INTERVAL, status, mbps)


def _point(at: datetime, mbps: float, status: str = "UP") -> Observation:
    return Observation(at, at, status, mbps)


def test_counters_that_recover_are_resolved_as_soon_as_they_are_seen() -> None:
    seen = [_point(T + timedelta(seconds=10), 0.0, "DOWN"), _point(T + timedelta(seconds=40), 1.8)]
    j = judge(seen, T, now=T + timedelta(seconds=45))
    assert j.verdict == "resolved"
    assert "1.80 Mbps" in j.detail


def test_zero_counters_wait_for_the_window_then_are_not_resolved() -> None:
    zero = [_bin(datetime(2026, 11, 7, 10, 15, tzinfo=UTC), 0.0)]
    assert judge(zero, T, now=T + timedelta(minutes=29)).verdict == "pending"
    j = judge(zero, T, now=T + VERIFY_WINDOW)
    assert j.verdict == "not_resolved"
    assert "stayed at zero" in j.detail


def test_no_data_is_unknown_once_the_window_has_closed() -> None:
    assert judge([], T, now=T + timedelta(minutes=5)).verdict == "pending"
    assert judge([], T, now=T + timedelta(minutes=31)).verdict == "unknown"


def test_only_observations_wholly_inside_the_window_count() -> None:
    """The 10:00 interval mixes before and after the action at 10:05; the 10:30 interval
    ends after the window. Neither may confirm the action."""
    outside = [
        _bin(datetime(2026, 11, 7, 10, 0, tzinfo=UTC), 1.8),
        _bin(datetime(2026, 11, 7, 10, 30, tzinfo=UTC), 1.8),
        _point(T - timedelta(seconds=5), 1.8),
    ]
    assert judge(outside, T, now=T + timedelta(hours=1)).verdict == "unknown"


def test_traffic_on_a_port_that_is_down_is_not_a_recovery() -> None:
    down = [_bin(datetime(2026, 11, 7, 10, 15, tzinfo=UTC), 1.8, "DOWN")]
    assert judge(down, T, now=T + timedelta(hours=1)).verdict == "not_resolved"


@pytest.mark.parametrize(
    ("support", "refute", "expected"),
    [(0, 0, 0.5), (1, 0, 2 / 3), (1, 1, 0.5), (0, 1, 1 / 3), (3, 1, 4 / 6)],
)
def test_confidence_is_the_beta_mean(support: int, refute: int, expected: float) -> None:
    assert beta_confidence(support, refute) == pytest.approx(expected)


def _cond(status: ConditionStatus) -> ConditionResult:
    return ConditionResult(name="x", status=status, detail="")


def test_conditions_met_only_when_every_condition_is_met() -> None:
    met, no, unk = ConditionStatus.MET, ConditionStatus.NOT_MET, ConditionStatus.UNKNOWN
    assert conditions_met([_cond(met), _cond(met)]) is True
    assert conditions_met([_cond(met), _cond(no)]) is False
    assert conditions_met([_cond(met), _cond(unk)]) is None
    assert conditions_met([]) is None


def _view(**kw: Any) -> AssertionView:
    base: dict[str, Any] = {
        "assertion_id": "ASR-X",
        "statement": "If the interface is UP with zero counters, bounce it.",
        "status": AssertionStatus.BELIEVED_TRUE,
        "valid_from": T,
        "recorded_at": T,
        "support_n": 1,
        "network_confirmed_on": ["TRN-017"],
        "bindings": [Binding(entity_id="TNC-1X", scope=Scope.APPLIES_TO)],
    }
    return AssertionView.model_validate(base | kw)


def test_the_card_shows_the_network_confirmation_and_the_confidence() -> None:
    assert evidence_line(_view()) == "network-confirmed on TRN-017 · support 1 · confidence 0.67"
    assert evidence_line(_view(network_confirmed_on=[], refute_n=1)) == (
        "support 1 · refute 1 · confidence 0.50"
    )
    assert evidence_line(_view(network_confirmed_on=[], support_n=0)) == ""

    thread = ThreadView(thread_id="slack:C1:1.0", channel="C1", last_message_ts=T)
    _, blocks = cap.card_blocks([_view()], thread, {})
    assert "network-confirmed on TRN-017 · support 1 · confidence 0.67" in json.dumps(
        blocks, ensure_ascii=False
    )


def test_agents_see_the_evidence_too() -> None:
    ctx = ContextResult(
        entity_id="TRN-017",
        as_of=T,
        known_at=T,
        entity_exists=True,
        scope_entities=["TRN-017"],
        assertions=[_view()],
    )
    assert "- Evidence: network-confirmed on TRN-017 · support 1" in context_markdown(ctx)
    recorded = OutcomeRecorded(
        event_id="OUT-1",
        assertion_id="ASR-X",
        evidence="SUPPORTS",
        conditions_met=True,
        verification=OutcomeVerification.RESOLVED,
        confidence=2 / 3,
    )
    assert summarize(recorded) == (
        "outcome OUT-1: evidence SUPPORTS; network resolved, confidence 0.67"
    )


@pytest.mark.parametrize(
    ("verification", "expected"),
    [
        ("resolved", "network-confirmed on TRN-017 1/1/2 · support 1 · refute 0 · confidence"),
        ("pending", "The network check is still running."),
        ("not_resolved", "stayed at zero after the action: network-checked"),
    ],
)
def test_the_outcome_note_says_network_confirmed_only_when_the_network_did(
    verification: str, expected: str
) -> None:
    out = {"support_n": 1, "refute_n": 0, "confidence": 0.67, "verification": verification}
    note = a._outcome_note(a.RUN_BOUNCE, "resolved", "TRN-017 1/1/2", "U5", out)
    assert expected in note
    if verification != "resolved":
        assert "network-confirmed" not in note
