"""R12 (plan 004): semantic ranking = max(0, cosine) x the structural score."""

from __future__ import annotations

from datetime import UTC, datetime

from silentctx.context.models import AssertionView
from silentctx.context.service import rank_semantic
from silentctx.domain.models import AssertionStatus

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _view(aid: str, status: AssertionStatus, confidence: str = "high") -> AssertionView:
    return AssertionView(
        assertion_id=aid,
        statement=aid,
        status=status,
        confidence=confidence,
        valid_from=T0,
        recorded_at=T0,
    )


def test_disputed_with_closer_wording_ranks_below_believed() -> None:
    believed = _view("A-believed", AssertionStatus.BELIEVED_TRUE)
    disputed = _view("A-disputed", AssertionStatus.DISPUTED)
    ranked = rank_semantic([disputed, believed], {"A-disputed": 1.0, "A-believed": 0.9})
    assert [a.assertion_id for a in ranked] == ["A-believed", "A-disputed"]
    assert ranked[0].score is not None
    assert ranked[1].score is not None
    assert ranked[0].score > ranked[1].score


def test_negative_cosine_is_clamped_and_unembedded_is_left_out() -> None:
    a = _view("A", AssertionStatus.BELIEVED_TRUE)
    b = _view("B", AssertionStatus.BELIEVED_TRUE)
    c = _view("C", AssertionStatus.BELIEVED_TRUE)
    ranked = rank_semantic([a, b, c], {"A": -0.4, "B": 0.2})
    assert [x.assertion_id for x in ranked] == ["B", "A"]
    assert ranked[1].score == 0.0
