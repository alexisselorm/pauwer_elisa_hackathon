"""Read-only Cypher for the console (UI-1). Nothing here writes, and nothing here is used by
agents: their reads go through ContextService and MCP."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, LiteralString

from neo4j import AsyncDriver

from silentctx.graph.repository import py
from silentctx.ui.models import GraphEdge, GraphNode, ToolCallView

_ASR = re.compile(r"\bASR-[0-9A-Z]+\b")
_ENTITY_KIND = {
    "Device": "device",
    "HwModel": "hw_model",
    "Generation": "generation",
    "Segment": "segment",
    "Site": "site",
    "Topic": "topic",
}
LABEL_CHARS = 42


def short(text: str, n: int = LABEL_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip(" ,;:.") + "…"


def _kind(labels: Sequence[str]) -> str | None:
    return next((_ENTITY_KIND[label] for label in labels if label in _ENTITY_KIND), None)


class UiQueries:
    def __init__(self, driver: AsyncDriver, database: str) -> None:
        self._driver = driver
        self._db = database

    async def _read(self, query: LiteralString, **params: Any) -> list[dict[str, Any]]:
        records, _, _ = await self._driver.execute_query(
            query, params, database_=self._db, routing_="r"
        )
        return [py(dict(r)) for r in records]

    # --- Graph ------------------------------------------------------------------------

    async def graph(self) -> tuple[list[GraphNode], list[GraphEdge]]:
        """All knowledge, the entities it is bound to (plus their model, generation and
        segment ends), and where it was said. The client filters by time."""
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}

        def edge(src: str, dst: str, rel: Any, **kw: Any) -> None:
            key = f"{rel}:{src}:{dst}"
            edges.setdefault(key, GraphEdge(id=key, source=src, target=dst, rel=rel, **kw))

        for r in await self._read(
            "MATCH (a:ContextAssertion) RETURN a { .assertion_id, .statement, .status, .origin, "
            ".topic, .valid_from, .valid_to, .recorded_at, .retracted_at, .support_n, "
            ".refute_n, .review_due, .source_type, .source_detail, .source_ref } AS a"
        ):
            a = r["a"]
            nodes[a["assertion_id"]] = GraphNode(
                id=a["assertion_id"],
                kind="assertion",
                label=short(a["statement"]),
                status=a["status"],
                origin=a.get("origin"),
                topic=a.get("topic"),
                valid_from=a["valid_from"],
                valid_to=a.get("valid_to"),
                recorded_at=a.get("recorded_at"),
                retracted_at=a.get("retracted_at"),
                support_n=a.get("support_n") or 0,
                refute_n=a.get("refute_n") or 0,
                review_due=bool(a.get("review_due")),
            )
            if a.get("source_type") and a.get("origin") == "gold":
                # Seeded history has no message events: its source is the cited record.
                sid = f"src:{a['source_type']}:{a.get('source_detail') or a.get('source_ref')}"
                detail = a.get("source_detail") or a["source_type"]
                nodes.setdefault(
                    sid,
                    GraphNode(
                        id=sid,
                        kind="source",
                        label=short(detail, 28),
                        sub=a["source_type"].replace("_", " "),
                        platform=_platform(a["source_type"]),
                        valid_from=a.get("recorded_at"),
                    ),
                )
                edge(a["assertion_id"], sid, "DERIVED_FROM")

        entity_ids: set[str] = set()
        for r in await self._read(
            "MATCH (a:ContextAssertion)-[b:ABOUT]->(e:Entity) "
            "RETURN a.assertion_id AS a, b.scope AS scope, coalesce(b.candidate, false) AS cand, "
            "e.entity_id AS id, labels(e) AS labels"
        ):
            entity_ids.add(r["id"])
            edge(r["a"], r["id"], "ABOUT", scope=r["scope"], candidate=r["cand"])

        # Each device's model and generation, each segment's ends: the network around it.
        for r in await self._read(
            "MATCH (d:Device)-[m:IS_MODEL]->(h:HwModel) WHERE d.entity_id IN $ids "
            "AND m.valid_to IS NULL RETURN d.entity_id AS d, h.entity_id AS h",
            ids=list(entity_ids),
        ):
            entity_ids.add(r["h"])
            edge(r["d"], r["h"], "IS_MODEL")
        for r in await self._read(
            "MATCH (s:Segment)-[:CONNECTS]-(d:Device) WHERE s.entity_id IN $ids "
            "RETURN s.entity_id AS s, d.entity_id AS d",
            ids=list(entity_ids),
        ):
            entity_ids.add(r["d"])
            edge(r["s"], r["d"], "CONNECTS")
        for r in await self._read(
            "MATCH (h:HwModel)-[:OF_GENERATION]->(g:Generation) WHERE h.entity_id IN $ids "
            "RETURN h.entity_id AS h, g.entity_id AS g",
            ids=list(entity_ids),
        ):
            entity_ids.add(r["g"])
            edge(r["h"], r["g"], "OF_GENERATION")

        for r in await self._read(
            "MATCH (e:Entity) WHERE e.entity_id IN $ids "
            "OPTIONAL MATCH (al:Alarm)-[:ON]->(e) "
            "RETURN e { .entity_id, .name, .label, .city, .model, .valid_from, .valid_to } AS e, "
            "labels(e) AS labels, count(al) AS alarms",
            ids=list(entity_ids),
        ):
            e, kind = r["e"], _kind(r["labels"])
            if kind is None:
                continue
            label = e["entity_id"]
            sub = None
            if kind == "device":
                sub = (e.get("city") or "").split(" ")[0] or None
            elif kind == "topic":
                label = (e.get("label") or label.removeprefix("TOPIC:")).replace("_", " ")
            elif kind == "segment":
                sub = short(e.get("name") or "", 24) or None
            nodes[e["entity_id"]] = GraphNode(
                id=e["entity_id"],
                kind=kind,
                label=label,
                sub=sub,
                valid_from=e.get("valid_from"),
                valid_to=e.get("valid_to"),
                alarms=r["alarms"],
            )

        for r in await self._read(
            "MATCH (a:ContextAssertion)-[r:SUPERSEDES|CORROBORATES|CONTRADICTS]->"
            "(b:ContextAssertion) RETURN a.assertion_id AS a, type(r) AS rel, b.assertion_id AS b"
        ):
            edge(r["a"], r["b"], r["rel"])

        # Captured knowledge: one source node per thread (Slack) or email conversation.
        for r in await self._read(
            "MATCH (a:ContextAssertion)-[:DERIVED_FROM]->(ev:Event) "
            "WITH a, coalesce(ev.thread_id, ev.event_id) AS src, ev "
            "RETURN a.assertion_id AS a, src, collect(ev.platform)[0] AS platform, "
            "collect(ev.channel)[0] AS channel, min(ev.event_time) AS at, "
            "collect(ev.text)[0] AS text"
        ):
            nodes.setdefault(
                r["src"],
                GraphNode(
                    id=r["src"],
                    kind="source",
                    label=short((r["text"] or r["src"]).split("\n")[0], 28),
                    sub=r["channel"],
                    platform=r["platform"],
                    valid_from=r["at"],
                ),
            )
            edge(r["a"], r["src"], "DERIVED_FROM")

        kept = [e for e in edges.values() if e.source in nodes and e.target in nodes]
        return list(nodes.values()), kept

    # --- Assertion sources -------------------------------------------------------------

    async def sources(self, assertion_id: str) -> list[dict[str, Any]]:
        return await self._read(
            "MATCH (:ContextAssertion {assertion_id: $id})-[d:DERIVED_FROM]->(ev:Event) "
            "RETURN ev.event_id AS event_id, ev.platform AS platform, ev.author AS author, "
            "ev.event_time AS event_time, d.quote AS quote, ev.text AS text, "
            "ev.permalink AS permalink, ev.thread_id AS thread_id, ev.channel AS channel "
            "ORDER BY ev.event_time",
            id=assertion_id,
        )

    async def assertion_extra(self, assertion_id: str) -> dict[str, Any] | None:
        rows = await self._read(
            "MATCH (a:ContextAssertion {assertion_id: $id}) "
            "RETURN a.qualifiers_json AS q, a.source_detail AS detail, a.asserted_by AS by",
            id=assertion_id,
        )
        return rows[0] if rows else None

    # --- Captures ----------------------------------------------------------------------

    async def threads(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self._read(
            "MATCH (t:Thread) "
            "OPTIONAL MATCH (m:Event) WHERE m.thread_id = t.thread_id "
            "WITH t, m ORDER BY m.event_time "
            "WITH t, collect(m) AS ms "
            "RETURN t { .thread_id, .platform, .channel, .status, .triage_kind, .triage_reason, "
            ".assertion_ids, .last_message_ts, .last_triaged_ts } AS t, "
            "t.capture_json IS NOT NULL AS has_trace, size(ms) AS n, "
            "CASE WHEN size(ms) > 0 THEN ms[0].text END AS first, "
            "CASE WHEN size(ms) > 0 THEN ms[0].event_time END AS started_at "
            "ORDER BY coalesce(t.last_message_ts, started_at) DESC LIMIT $limit",
            limit=limit,
        )

    async def thread(self, thread_id: str) -> dict[str, Any] | None:
        rows = await self._read(
            "MATCH (t:Thread {thread_id: $id}) "
            "OPTIONAL MATCH (m:Event) WHERE m.thread_id = t.thread_id AND m.text IS NOT NULL "
            "WITH t, m ORDER BY m.event_time "
            "RETURN t { .thread_id, .platform, .channel, .status, .triage_kind, .triage_reason, "
            ".assertion_ids, .last_message_ts, .last_triaged_ts, .capture_json } AS t, "
            "collect(m { .event_id, .author, .event_time, .text, .permalink }) AS messages",
            id=thread_id,
        )
        return rows[0] if rows else None

    # --- Agent runs --------------------------------------------------------------------

    async def tool_calls(self, since: datetime, limit: int = 1000) -> list[ToolCallView]:
        rows = await self._read(
            "MATCH (c:ToolCall) WHERE c.at >= $since RETURN c {.*} AS c ORDER BY c.at DESC "
            "LIMIT $limit",
            since=since,
            limit=limit,
        )
        out = []
        for r in reversed(rows):
            c = r["c"]
            try:
                args = json.loads(c.get("args_json") or "{}")
            except ValueError:
                args = {}
            out.append(
                ToolCallView(
                    call_id=c["call_id"],
                    agent_id=c.get("agent_id") or "unknown",
                    tool=c["tool"],
                    args=args,
                    ok=bool(c.get("ok")),
                    summary=c.get("summary") or "",
                    ms=c.get("ms") or 0,
                    at=c["at"],
                    cited=list(dict.fromkeys(_ASR.findall(c.get("summary") or ""))),
                )
            )
        return out

    # --- Live stream -------------------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        """What the console shows, as small fingerprints by id. The live stream diffs two
        snapshots; `recorded_at` can't be used for "new" because it is the message time."""
        rows = await self._read(
            "CALL () { MATCH (a:ContextAssertion) RETURN collect([a.assertion_id, "
            "  a.status + '|' + coalesce(toString(a.valid_to), '') + '|' + "
            "  coalesce(toString(a.retracted_at), '') + '|' + "
            "  toString(coalesce(a.support_n, 0)) + '/' + toString(coalesce(a.refute_n, 0))]) "
            "  AS assertions } "
            "CALL () { MATCH (t:Thread) RETURN collect([t.thread_id, coalesce(t.status, '') + "
            "  '|' + coalesce(toString(t.last_triaged_ts), '') + '|' + "
            "  coalesce(toString(t.last_message_ts), '')]) AS threads } "
            "CALL () { MATCH ()-[r:CORROBORATES|CONTRADICTS|SUPERSEDES]->() "
            "  RETURN count(r) AS relations } "
            "CALL () { MATCH (c:ToolCall) RETURN count(c) AS calls } "
            "RETURN assertions, threads, relations, calls"
        )
        r = rows[0]
        return {
            "assertions": {k: v for k, v in r["assertions"]},
            "threads": {k: v for k, v in r["threads"]},
            "relations": r["relations"],
            "calls": r["calls"],
        }


def _platform(source_type: str) -> str:
    s = source_type.lower()
    if "email" in s:
        return "email"
    if "teams" in s or "slack" in s or "chat" in s:
        return "chat"
    return "record"


RUN_GAP = timedelta(seconds=90)
