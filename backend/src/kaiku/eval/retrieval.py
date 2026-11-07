"""Retrieval eval (plan 004 §6): `search_context` alone, on the gold graph, no LLM.

The QA sets score the agent's final answer, so a retrieval miss only shows up there as a
wrong answer. This set scores the ranked list itself, per question:
- recall@k: the share of the `expected` assertions in the top k;
- reciprocal rank of the first expected one (MRR over the set);
- forbidden: nothing invalid at `as_of`, or inapplicable to the entity asked about, is
  returned at all.

An item passes when every expected assertion is in the top k and nothing forbidden is
returned. Deterministic: exact ids, no judge.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, SecretStr

from kaiku.config import Settings
from kaiku.context.service import ContextService
from kaiku.eval.score import Check, ItemResult, SetResult, field_accuracy, iso
from kaiku.graph import schema
from kaiku.graph.driver import create_driver
from kaiku.graph.repository import ContextRepository
from kaiku.llm.embeddings import (
    EmbeddingDimensionError,
    EmbeddingUnavailableError,
    FeatherlessEmbedder,
)
from kaiku.logging import get_logger
from kaiku.seed.profiles import Profile
from kaiku.seed.service import SeedRequest, seed
from kaiku.seed.sources import MockData
from kaiku.telemetry.store import TelemetryStore

log = get_logger(__name__)
DEFAULT_K = 5
RESULT_LIMIT = 10  # what an agent asks for by default (the tool's own default)
SNAPSHOT = datetime(2026, 11, 7, tzinfo=UTC)  # "now" for the gold graph, as in the eval


class RetrievalQuestion(BaseModel):
    id: str
    query: str
    as_of: str
    entity_id: str | None = None
    expected: list[str] = []
    forbidden: list[str] = []
    # time: not valid at as_of. scope: valid, but not for the entity asked about (R9).
    forbidden_why: Literal["time", "scope"] = "time"
    tests: str = ""


def load_questions(path: Path) -> list[RetrievalQuestion]:
    return [
        RetrievalQuestion.model_validate_json(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


class RetrievalScore(BaseModel):
    recall: float | None  # None when nothing is expected (a must-be-empty item)
    reciprocal_rank: float | None
    item: ItemResult


def score_ranking(q: RetrievalQuestion, ranked: Sequence[str], k: int) -> RetrievalScore:
    top = list(ranked[:k])
    checks: list[Check] = []
    recall = rr = None
    if q.expected:
        found = [i for i in q.expected if i in top]
        recall = len(found) / len(q.expected)
        first = next((n for n, i in enumerate(ranked, 1) if i in q.expected), None)
        rr = 1 / first if first else 0.0
        missing = sorted(set(q.expected) - set(found))
        checks.append(
            Check(
                name=f"recall@{k}",
                passed=not missing,
                detail=f"missing {', '.join(missing)}; got {', '.join(top) or 'nothing'}"
                if missing
                else "",
            )
        )
    leaked = [i for i in ranked if i in q.forbidden]
    checks.append(
        Check(
            name="forbidden",
            passed=not leaked,
            detail=f"returned {', '.join(leaked)}" if leaked else "",
        )
    )
    return RetrievalScore(
        recall=recall, reciprocal_rank=rr, item=ItemResult.of(q.id, checks, note=q.tests)
    )


def summarise(name: str, scores: Sequence[RetrievalScore], k: int, mode: str) -> SetResult:
    result = SetResult(name=name, items=[s.item for s in scores])
    result.field_accuracy = field_accuracy(result.items)
    recalls = [s.recall for s in scores if s.recall is not None]
    rrs = [s.reciprocal_rank for s in scores if s.reciprocal_rank is not None]
    if recalls:
        result.field_accuracy[f"mean recall@{k}"] = f"{sum(recalls) / len(recalls):.2f}"
    if rrs:
        result.field_accuracy["MRR"] = f"{sum(rrs) / len(rrs):.2f}"
    result.field_accuracy["mode"] = mode
    return result


async def evaluate(
    svc: ContextService, questions: Sequence[RetrievalQuestion], k: int = DEFAULT_K
) -> SetResult:
    """Ask every question through `search_context` and score the ranked ids."""
    scores: list[RetrievalScore] = []
    modes: set[str] = set()
    for q in questions:
        res = await svc.search_context(
            q.query, as_of=iso(q.as_of), entity_id=q.entity_id, limit=RESULT_LIMIT
        )
        modes.add(res.mode)
        scores.append(score_ranking(q, [a.assertion_id for a in res.assertions], k))
    return summarise("R retrieval", scores, k, "/".join(sorted(modes)) or "none")


async def run_retrieval(
    base: Settings,
    *,
    questions: Path,
    k: int = DEFAULT_K,
    neo4j_uri: str | None = None,
    neo4j_password: str | None = None,
) -> tuple[SetResult, dict[str, str | None]]:
    """Seed the gold graph into an isolated Neo4j and run the set. Returns the scored set
    and the models used (the embedding model, or None in keyword mode)."""
    from kaiku.eval.runner import _neo4j  # the runner's isolated-Neo4j helper

    items = load_questions(questions)
    with _neo4j(neo4j_uri, neo4j_password) as (uri, password):
        settings = base.model_copy(
            update={"neo4j_uri": uri, "neo4j_password": SecretStr(password), "sim_url": None}
        )
        await schema.apply_migrations(settings)
        seeded = await seed(settings, SeedRequest(network=True, gold=True, profile=Profile.FULL))
        log.info("retrieval_eval_seeded", violations=seeded.violations)
        embedder = FeatherlessEmbedder.from_settings(settings)
        driver = create_driver(settings)
        try:
            svc = ContextService(
                ContextRepository(driver, settings.neo4j_database),
                TelemetryStore(settings.data_dir),
                embedder=embedder,
                aliases=MockData(settings.data_dir).model_aliases(),
                clock=lambda: SNAPSHOT,
            )
            try:
                await svc.embed_missing()
            except (EmbeddingUnavailableError, EmbeddingDimensionError) as exc:
                log.warning("retrieval_eval_embedding_failed", error=str(exc))
            result = await evaluate(svc, items, k)
        finally:
            await driver.close()
    models = {"embedding": settings.embedding_model if embedder is not None else None}
    return result, models
