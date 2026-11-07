"""The eval scorer's own rules (plan 003 §2.3): no LLM, no graph."""

from datetime import UTC, datetime

from kaiku.context.models import AssertionView, Binding
from kaiku.eval.score import ExpectedAssertion, match_all, same_status, scoped, status_at

T = datetime(2026, 11, 7, tzinfo=UTC)


def _view(**kw: object) -> AssertionView:
    base: dict[str, object] = {
        "assertion_id": "A-1",
        "statement": "s",
        "status": "unverified",
        "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
        "recorded_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return AssertionView.model_validate(base | kw)


def test_topics_are_scored_by_name_and_candidates_are_left_out() -> None:
    v = _view(
        bindings=[
            Binding(entity_id="TOPIC:sfp_replacement", scope="activity"),
            Binding(entity_id="TOPIC:other", scope="activity", candidate=True),
        ]
    )
    assert scoped(v, "activity") == {"sfp_replacement"}


def test_unverified_and_believed_true_both_read_as_current() -> None:
    assert same_status("unverified", "believed_true")
    assert same_status("believed_true", "unverified")
    assert not same_status("unverified", "superseded")
    assert not same_status("disputed", "believed_true")


def test_status_at_snapshot_follows_valid_to() -> None:
    assert status_at(_view(valid_to=datetime(2026, 11, 1, tzinfo=UTC)), T) == "expired"
    assert status_at(_view(status="superseded", valid_to=datetime(2026, 12, 1, tzinfo=UTC)), T) == (
        "believed_true"
    )


def test_each_expected_assertion_gets_its_own_capture() -> None:
    """H-06: both sides of the dispute mention "bounce" and "reseat"."""
    on_019 = [Binding(entity_id="TRN-019", scope="observed_on")]
    reseat = _view(
        assertion_id="A-reseat", statement="Bounce fails; reseat the card", bindings=on_019
    )
    bounce = _view(
        assertion_id="A-bounce", statement="Bounce worked; reseat takes it down", bindings=on_019
    )
    exp = [
        ExpectedAssertion(key="a", observed_on=["TRN-019"], statement_contains=["reseat"]),
        ExpectedAssertion(key="b", observed_on=["TRN-019"], statement_contains=["bounce"]),
    ]
    got = match_all(exp, [reseat, bounce])
    assert {got["a"].assertion_id, got["b"].assertion_id} == {"A-reseat", "A-bounce"}  # type: ignore[union-attr]
    only_one = match_all(exp, [reseat])
    assert sum(v is not None for v in only_one.values()) == 1
