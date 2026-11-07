"""The retrieval eval set and its scoring (plan 004 §6)."""

from __future__ import annotations

import json
from pathlib import Path

from kaiku.eval.retrieval import RetrievalQuestion, load_questions, score_ranking, summarise
from kaiku.eval.score import iso

ROOT = Path(__file__).parents[2]
QUESTIONS = ROOT / "eval" / "retrieval" / "questions.jsonl"
GOLD = ROOT / "mock_data" / "knowledge_graph" / "assertions.jsonl"


def _gold() -> dict[str, dict[str, str | None]]:
    rows = [json.loads(line) for line in GOLD.read_text("utf-8").splitlines() if line.strip()]
    return {r["assertion_id"]: r for r in rows}


def test_set_matches_the_gold_validity_windows() -> None:
    """Every expected id is valid at as_of and every time-forbidden one is not, by the gold
    file's own valid_from / valid_to: the set can't drift from the data it scores."""
    gold = _gold()
    questions = load_questions(QUESTIONS)
    assert len({q.id for q in questions}) == len(questions)
    for q in questions:
        t = iso(q.as_of)
        assert t is not None
        assert not set(q.expected) & set(q.forbidden), q.id
        for aid in [*q.expected, *q.forbidden]:
            assert aid in gold, f"{q.id}: unknown {aid}"
            vf, vt = iso(gold[aid]["valid_from"]), iso(gold[aid]["valid_to"])
            assert vf is not None
            valid = vf <= t and (vt is None or t < vt)
            should_be = aid in q.expected or q.forbidden_why == "scope"
            assert valid == should_be, f"{q.id}: {aid} valid={valid} at {q.as_of}"


def _q(expected: list[str], forbidden: list[str] | None = None) -> RetrievalQuestion:
    return RetrievalQuestion(
        id="RQ", query="q", as_of="2026-01-01T00:00:00Z", expected=expected,
        forbidden=forbidden or [],
    )  # fmt: skip


def test_score_ranking_recall_rank_and_forbidden() -> None:
    s = score_ranking(_q(["A", "B"], ["X"]), ["C", "A", "D", "E", "F", "B"], k=5)
    assert s.recall == 0.5
    assert s.reciprocal_rank == 0.5
    assert not s.item.passed
    assert "missing B" in s.item.checks[0].detail

    s = score_ranking(_q(["A"], ["X"]), ["A", "C", "X"], k=5)
    assert s.recall == 1.0
    assert not s.item.passed  # X was not valid at as_of and came back anyway
    assert s.item.checks[1].detail == "returned X"

    s = score_ranking(_q([], ["X"]), ["A"], k=5)  # a must-be-absent item
    assert s.recall is None
    assert s.item.passed


def test_summarise_reports_means_and_mode() -> None:
    scores = [
        score_ranking(_q(["A"]), ["A"], k=5),
        score_ranking(_q(["A"]), ["B", "C"], k=5),
        score_ranking(_q([], ["X"]), [], k=5),
    ]
    result = summarise("R", scores, 5, "keyword")
    assert (result.passed, result.scored) == (2, 3)
    assert result.field_accuracy["mean recall@5"] == "0.50"
    assert result.field_accuracy["MRR"] == "0.50"
    assert result.field_accuracy["mode"] == "keyword"
