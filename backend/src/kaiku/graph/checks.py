"""The temporal invariants (plan 001 §3.3, §6.1 V5), as Cypher in `checks.cypher`.

They run in three places:
- on the whole graph after a seed and in `kaiku eval time` (`run_checks`);
- inside every knowledge write, on the assertions it touched (`write_violations`, called by
  the repository's V5 step, which rolls the write back on any violation);
- after every ingest, on the stored events and their threads (`run_checks` with a scope).

Each check returns the offending ids; an empty result means the graph satisfies it.
Windows are half-open [valid_from, valid_to), null valid_to = open-ended.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal, LiteralString

from neo4j import READ_ACCESS, AsyncDriver, AsyncManagedTransaction
from pydantic import BaseModel

CHECKS_FILE = Path(__file__).parent / "checks.cypher"

# What a check's $ids are matched against. A [graph] check reads the whole graph only.
Anchor = Literal["assertion", "event", "thread", "graph"]
ANCHORS: tuple[Anchor, ...] = ("assertion", "event", "thread", "graph")
_HEADER = re.compile(r"^// check: (\w+) \[(\w+)\]\s*$", re.MULTILINE)


class CheckResult(BaseModel):
    name: str
    violations: int
    examples: list[str]


class Check(BaseModel):
    name: str
    anchor: Anchor
    query: str


def load_checks(path: Path = CHECKS_FILE) -> dict[str, Check]:
    """Parse `checks.cypher`: each "// check: <name> [<anchor>]" header starts a query that
    runs to the next header. Comment lines are kept (Cypher ignores them)."""
    text = path.read_text(encoding="utf-8")
    headers = list(_HEADER.finditer(text))
    checks: dict[str, Check] = {}
    for header, following in zip(headers, [*headers[1:], None], strict=True):
        name, anchor = header.group(1), header.group(2)
        if anchor not in ANCHORS:
            raise ValueError(f"{path.name}: check {name!r} has unknown anchor [{anchor}]")
        if name in checks:
            raise ValueError(f"{path.name}: check {name!r} is defined twice")
        end = following.start() if following else len(text)
        lines = text[header.end() : end].strip().splitlines()
        while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("//")):
            lines.pop()  # a section heading before the next check
        query = "\n".join(lines)
        checks[name] = Check.model_validate({"name": name, "anchor": anchor, "query": query})
    return checks


CHECKS: dict[str, Check] = load_checks()

# anchor -> ids: assertion_id for "assertion", event_id for "event", thread_id for "thread".
Scope = Mapping[str, Sequence[str]]


async def _run(
    tx: AsyncManagedTransaction, scope: Scope | None, now: datetime | None, limit: int
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in CHECKS.values():
        ids: list[str] | None = None
        if scope is not None:
            ids = sorted(set(scope.get(check.anchor, ()))) if check.anchor != "graph" else []
            if not ids:
                continue
        # The queries come from the package's own checks.cypher, never from input.
        query: LiteralString = check.query
        rows = await (await tx.run(query, ids=ids, now=now)).data()
        found = sorted(str(r["id"]) for r in rows)
        results.append(CheckResult(name=check.name, violations=len(found), examples=found[:limit]))
    return results


async def run_checks(
    driver: AsyncDriver,
    database: str,
    limit: int = 5,
    *,
    scope: Scope | None = None,
    now: datetime | None = None,
) -> list[CheckResult]:
    """Run the checks in one read transaction. With no scope, every check reads the whole
    graph; with a scope, only the checks anchored on a named kind run, on those ids.
    `now` is what the status checks call the present (default: the database clock)."""
    async with driver.session(database=database, default_access_mode=READ_ACCESS) as session:

        async def work(tx: AsyncManagedTransaction) -> list[CheckResult]:
            return await _run(tx, scope, now, limit)

        return await session.execute_read(work)


async def write_violations(tx: AsyncManagedTransaction, assertion_ids: Sequence[str]) -> list[str]:
    """The assertion checks on the assertions a write touched, inside its transaction (V5).
    Returns "check: id" for every violation (uncapped, so two reads compare exactly); empty
    when the write may commit."""
    results = await _run(tx, {"assertion": assertion_ids}, None, limit=10_000)
    return [f"{r.name}: {example}" for r in results for example in r.examples]
