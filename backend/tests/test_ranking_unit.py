"""R12 (plan 004): ranking = max(0, similarity) x the structural score; keyword similarity."""

from __future__ import annotations

from datetime import UTC, datetime

from kaiku.context.models import AssertionView
from kaiku.context.service import lexical_similarity, query_terms, rank_by_similarity
from kaiku.domain.models import AssertionStatus

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _view(
    aid: str, status: AssertionStatus, confidence: str = "high", statement: str | None = None
) -> AssertionView:
    return AssertionView(
        assertion_id=aid,
        statement=statement or aid,
        status=status,
        confidence=confidence,
        valid_from=T0,
        recorded_at=T0,
    )


def test_disputed_with_closer_wording_ranks_below_believed() -> None:
    believed = _view("A-believed", AssertionStatus.BELIEVED_TRUE)
    disputed = _view("A-disputed", AssertionStatus.DISPUTED)
    ranked = rank_by_similarity(
        [disputed, believed], {"A-disputed": 1.0, "A-believed": 0.9}, keep_unmatched=True
    )
    assert [a.assertion_id for a in ranked] == ["A-believed", "A-disputed"]
    assert ranked[0].score is not None
    assert ranked[1].score is not None
    assert ranked[0].score > ranked[1].score


def test_negative_cosine_is_clamped_and_semantic_mode_keeps_every_candidate() -> None:
    a = _view("A", AssertionStatus.BELIEVED_TRUE)
    b = _view("B", AssertionStatus.BELIEVED_TRUE)
    c = _view("C", AssertionStatus.BELIEVED_TRUE)
    ranked = rank_by_similarity([a, b, c], {"A": -0.4, "B": 0.2}, keep_unmatched=True)
    assert [x.assertion_id for x in ranked] == ["B", "A", "C"]
    assert ranked[1].score == 0.0


def test_keyword_mode_drops_candidates_sharing_no_term() -> None:
    a = _view("A", AssertionStatus.BELIEVED_TRUE)
    b = _view("B", AssertionStatus.BELIEVED_TRUE)
    ranked = rank_by_similarity([a, b], {"A": 0.0, "B": 0.5}, keep_unmatched=False)
    assert [x.assertion_id for x in ranked] == ["B"]


def test_keyword_ranking_weighs_status_not_just_term_count() -> None:
    """The old fallback kept whatever matched any term, in database order."""
    terms = query_terms("What to do about zero traffic after an SFP swap?")
    believed = _view(
        "A-rule",
        AssertionStatus.BELIEVED_TRUE,
        statement="Zero traffic after an SFP swap: bounce the interface.",
    )
    disputed = _view(
        "A-report",
        AssertionStatus.DISPUTED,
        statement="Zero traffic after an SFP swap; bounce failed, replaced the SFP.",
    )
    unrelated = _view("A-mtu", AssertionStatus.BELIEVED_TRUE, statement="Core links are MTU 9216.")
    sim = {v.assertion_id: lexical_similarity(terms, v) for v in (believed, disputed, unrelated)}
    ranked = rank_by_similarity([unrelated, disputed, believed], sim, keep_unmatched=False)
    assert [x.assertion_id for x in ranked] == ["A-rule", "A-report"]


def test_lexical_similarity_matches_words_and_long_prefixes_only() -> None:
    v = _view("A", AssertionStatus.BELIEVED_TRUE, statement="SFP replacement on upgraded units")
    v = v.model_copy(update={"topic": "forwarding_state_after_sfp_swap"})
    assert query_terms("Is the SFP swap up?") == ["sfp", "swap", "up"]
    # "sfp" and "swap" (from the topic) match; "up" is too short to match "upgraded".
    assert lexical_similarity(["sfp", "swap", "up"], v) == 2 / 3
    assert lexical_similarity(["replac"], v) == 1.0
    assert lexical_similarity([], v) == 0.0
