"""The ranking policy (plan 002 §4.1 "ranked", plan 004 R12): how an assertion reached the
entity, its status and its confidence. Shown to agents as `score`; never used to hide
anything. The repository and the search both rank with it, so it lives in one place.
"""

from __future__ import annotations

from kaiku.context.models import AssertionView, Match
from kaiku.domain.models import AssertionStatus

MATCH_WEIGHT: dict[Match, float] = {
    Match.DIRECT: 1.0,
    Match.MODEL: 0.9,
    Match.SITE: 0.85,
    Match.SEGMENT: 0.8,
    Match.SAME_MODEL_DEVICE: 0.5,
    # Below another device of the same model: when the query names an entity, knowledge
    # about unrelated ones is the least likely to apply (plan 004 R12).
    Match.OUTSIDE_SCOPE: 0.4,
}
STATUS_WEIGHT: dict[AssertionStatus, float] = {
    AssertionStatus.BELIEVED_TRUE: 1.0,
    AssertionStatus.UNVERIFIED: 0.8,
    AssertionStatus.DISPUTED: 0.7,
    # Everything ranked has already passed the validity filter at t (R9, R12 plan 004), so
    # "superseded" / "expired" only say what happened *after* t. At a past as_of that
    # assertion is the one in force; a penalty would push the right answer down.
    AssertionStatus.SUPERSEDED: 1.0,
    AssertionStatus.EXPIRED: 1.0,
}
CONFIDENCE_WEIGHT: dict[str, float] = {"high": 1.0, "medium": 0.85, "low": 0.6}
DEFAULT_MATCH_WEIGHT = 0.7
DEFAULT_STATUS_WEIGHT = 0.7
DEFAULT_CONFIDENCE_WEIGHT = 0.8
REVIEW_DUE_WEIGHT = 0.9

# The match order, best first, follows the weights: an assertion reached from two scope
# entities keeps the better match.
MATCH_ORDER: tuple[Match, ...] = tuple(
    sorted(MATCH_WEIGHT, key=lambda m: (-MATCH_WEIGHT[m], m.value))
)
MATCH_RANK: dict[Match, int] = {m: i for i, m in enumerate(MATCH_ORDER)}
MATCH_BY_RANK: dict[int, Match] = dict(enumerate(MATCH_ORDER))


def score(view: AssertionView) -> float:
    """match x status x confidence, x0.9 when a review is due. In [0, 1]."""
    conf = view.confidence
    if isinstance(conf, int | float):
        conf_w = 0.5 + 0.5 * float(conf)
    else:
        conf_w = CONFIDENCE_WEIGHT.get(str(conf), DEFAULT_CONFIDENCE_WEIGHT)
    s = MATCH_WEIGHT.get(view.match, DEFAULT_MATCH_WEIGHT) if view.match else DEFAULT_MATCH_WEIGHT
    s *= STATUS_WEIGHT.get(view.status, DEFAULT_STATUS_WEIGHT) * conf_w
    if view.review_due:
        s *= REVIEW_DUE_WEIGHT
    return s
