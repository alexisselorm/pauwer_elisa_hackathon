"""Vector ranking (spike BE-1.2, verified on neo4j:2026.10.1-community).

Findings:
- `MATCH (n) SEARCH n IN (VECTOR INDEX i FOR $v LIMIT k) SCORE AS s` works (Cypher 25 is
  the server default). `db.index.vector.queryNodes` still works but is deprecated.
- `WHERE` *inside* SEARCH pre-filters, but only on properties the index declares with
  `WITH [n.prop]` (migration V0002 declares `status` on `assertion_embedding`).
- A `WHERE` before or after SEARCH filters the top-k AFTER the search. Scoping to a set of
  entities that way silently drops matches. So scoped retrieval does NOT use the index:
  the graph narrows the candidates first (by entity and time, plan 001), then
  `similarities` scores every one of them exactly with `vector.similarity.cosine` and the
  caller ranks (plan 004 R12: nothing is cut before the structural score is applied).

Labels and index names can't be query parameters, so they come only from the fixed
`VectorIndexSpec` constants below, never from callers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import LiteralString

from neo4j import AsyncDriver
from neo4j.exceptions import CypherSyntaxError
from pydantic import BaseModel, Field

from kaiku.logging import get_logger

MAX_K = 100
log = get_logger(__name__)


@dataclass(frozen=True)
class VectorIndexSpec:
    index: LiteralString
    label: LiteralString
    id_property: LiteralString
    filterable: tuple[str, ...] = ()


ASSERTIONS = VectorIndexSpec(
    index="assertion_embedding",
    label="ContextAssertion",
    id_property="assertion_id",
    filterable=("status",),
)
CHUNKS = VectorIndexSpec(index="chunk_embedding", label="SourceChunk", id_property="chunk_id")


class VectorHit(BaseModel):
    id: str
    score: float = Field(ge=-1.0, le=1.0)


def _check_k(k: int) -> None:
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k must be between 1 and {MAX_K}")


async def search(
    driver: AsyncDriver,
    database: str,
    spec: VectorIndexSpec,
    vector: Sequence[float],
    k: int,
    *,
    status_in: Sequence[str] | None = None,
) -> list[VectorHit]:
    """Global top-k through the vector index, optionally pre-filtered by status.
    Falls back to the deprecated procedure if SEARCH isn't available on the server."""
    _check_k(k)
    if status_in is not None and "status" not in spec.filterable:
        raise ValueError(f"{spec.index} can't filter on status")
    try:
        return await _search_clause(driver, database, spec, vector, k, status_in)
    except CypherSyntaxError:
        log.warning("vector_search_clause_unsupported_using_procedure", index=spec.index)
        return await _search_procedure(driver, database, spec, vector, k, status_in)


async def _search_clause(
    driver: AsyncDriver,
    database: str,
    spec: VectorIndexSpec,
    vector: Sequence[float],
    k: int,
    status_in: Sequence[str] | None,
) -> list[VectorHit]:
    where: LiteralString = "WHERE n.status IN $status_in " if status_in is not None else ""
    query: LiteralString = (
        "MATCH (n:" + spec.label + ") "
        "SEARCH n IN (VECTOR INDEX " + spec.index + " FOR $vector " + where + "LIMIT $k) "
        "SCORE AS score "
        "RETURN n." + spec.id_property + " AS id, score ORDER BY score DESC"
    )
    records, _, _ = await driver.execute_query(
        query, vector=list(vector), k=k, status_in=list(status_in or []), database_=database
    )
    return [VectorHit.model_validate(dict(r)) for r in records]


async def _search_procedure(
    driver: AsyncDriver,
    database: str,
    spec: VectorIndexSpec,
    vector: Sequence[float],
    k: int,
    status_in: Sequence[str] | None,
) -> list[VectorHit]:
    """Fallback. The procedure can't pre-filter, so it over-fetches and filters after;
    with a filter it may return fewer than k hits."""
    fetch = min(k * 5, MAX_K * 5) if status_in is not None else k
    where: LiteralString = "WHERE node.status IN $status_in " if status_in is not None else ""
    query: LiteralString = (
        "CALL db.index.vector.queryNodes($index, $fetch, $vector) YIELD node, score "
        + where
        + "RETURN node."
        + spec.id_property
        + " AS id, score ORDER BY score DESC LIMIT $k"
    )
    records, _, _ = await driver.execute_query(
        query,
        index=spec.index,
        fetch=fetch,
        vector=list(vector),
        k=k,
        status_in=list(status_in or []),
        database_=database,
    )
    return [VectorHit.model_validate(dict(r)) for r in records]


async def similarities(
    driver: AsyncDriver,
    database: str,
    spec: VectorIndexSpec,
    ids: Sequence[str],
    vector: Sequence[float],
) -> list[VectorHit]:
    """Exact cosine for every candidate that has an embedding, with no top-k cut: the
    caller combines it with other weights before choosing what to keep (R12, plan 004).

    Raw cosine in [-1, 1]. Neo4j's `vector.similarity.cosine` returns (1 + cos) / 2, so an
    unrelated statement (cos 0) would score 0.5 and a confidence or status weight would
    outweigh the wording; the query maps it back."""
    if not ids:
        return []
    query: LiteralString = (
        "MATCH (n:" + spec.label + ") WHERE n." + spec.id_property + " IN $ids "
        "AND n.embedding IS NOT NULL "
        "RETURN n." + spec.id_property + " AS id, "
        "2 * vector.similarity.cosine(n.embedding, $vector) - 1 AS score"
    )
    records, _, _ = await driver.execute_query(
        query, ids=list(ids), vector=list(vector), database_=database
    )
    return [VectorHit.model_validate(dict(r)) for r in records]
