"""The console API (UI-1): read-only views of the graph for the web console, at /api/ui.

People capture and ask in Slack; agents read and write through MCP. The console only shows
what happened: the knowledge graph over time, each capture's gates, and agents' MCP calls.
A server-sent-events stream tells the browser when to refetch.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from sse_starlette import EventSourceResponse

from silentctx.config import Settings
from silentctx.context.models import EntityView
from silentctx.context.service import ContextError, ContextService
from silentctx.extraction.graph import CaptureResult
from silentctx.logging import get_logger
from silentctx.ui import gates
from silentctx.ui.models import (
    AgentRun,
    AssertionDetail,
    CaptureDetail,
    CaptureSummary,
    Change,
    EvalReportView,
    GraphOut,
    Meta,
    SourceQuote,
    ThreadMessage,
    ToolCallView,
)
from silentctx.ui.names import SlackNames
from silentctx.ui.queries import RUN_GAP, UiQueries, short

log = get_logger(__name__)
router = APIRouter(prefix="/api/ui", tags=["console"])

_MENTION = re.compile(r"<@([UW][A-Z0-9]+)>")
_LINK = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
WRITE_TOOLS = {"report_outcome", "open_knowledge_gap", "propose_assertion", "flag_assertion"}


@dataclass(frozen=True)
class Console:
    settings: Settings
    service: ContextService
    queries: UiQueries
    names: SlackNames


def get_console(request: Request) -> Console:
    console: Console | None = getattr(request.app.state, "console", None)
    if console is None:
        raise HTTPException(503, "the console is starting")
    return console


ConsoleDep = Annotated[Console, Depends(get_console)]


async def _clean(text: str, names: SlackNames) -> str:
    """Slack markup -> readable text: <@U123> -> @Name, <url|label> -> label."""
    for uid in set(_MENTION.findall(text)):
        text = text.replace(f"<@{uid}>", f"@{await names.user(uid)}")
    return _LINK.sub(r"\2", text)


@router.get("/meta")
async def meta(console: ConsoleDep) -> Meta:
    s = console.settings
    return Meta(
        now=datetime.now(UTC),
        phoenix_url=s.phoenix_public_url,
        phoenix_project=s.phoenix_project_name,
    )


@router.get("/graph")
async def graph(console: ConsoleDep) -> GraphOut:
    """Every assertion with its validity, the entities it is bound to and its sources.
    Time filtering (as of t, known at k) happens in the browser, so the slider is instant."""
    nodes, edges = await console.queries.graph()
    for n in nodes:
        if n.kind == "source" and n.sub:
            n.sub = await console.names.channel(n.sub)
    return GraphOut(now=datetime.now(UTC), nodes=nodes, edges=edges)


@router.get("/assertions/{assertion_id}")
async def assertion(assertion_id: str, console: ConsoleDep) -> AssertionDetail:
    views = await console.service.repo.assertions([assertion_id])
    if not views:
        raise HTTPException(404, f"unknown assertion {assertion_id!r}")
    history = await console.service.get_history(assertion_id)
    extra = await console.queries.assertion_extra(assertion_id) or {}
    sources = [
        SourceQuote(
            **{
                **s,
                "author": await console.names.user(s.get("author")),
                "channel": await console.names.channel(s.get("channel")),
                "text": await _clean(s.get("text") or "", console.names),
            }
        )
        for s in await console.queries.sources(assertion_id)
    ]
    thread_id = views[0].qualifiers.get("thread_id") or next(
        (s.thread_id for s in sources if s.thread_id), None
    )
    return AssertionDetail(
        assertion=views[0],
        sources=sources,
        versions=history.versions,
        contradicts=history.contradicts,
        corroborates=history.corroborates,
        evidence=history.evidence,
        flags=history.flags,
        thread_id=thread_id,
        source_detail=extra.get("detail"),
        asserted_by=await console.names.user(extra.get("by")),
    )


@router.get("/entities/{entity_id}")
async def entity(entity_id: str, console: ConsoleDep, as_of: datetime | None = None) -> EntityView:
    try:
        return await console.service.get_entity(entity_id, as_of)
    except ContextError as exc:
        raise HTTPException(404, str(exc)) from exc


async def _summary(
    th: dict[str, Any], console: Console, first: str | None, **kw: Any
) -> CaptureSummary:
    text = await _clean(first or "", console.names)
    return CaptureSummary(
        thread_id=th["thread_id"],
        platform=th.get("platform"),
        channel=await console.names.channel(th.get("channel")),
        title=short(text.split("\n")[0], 90) or th["thread_id"],
        last_message_ts=th.get("last_message_ts"),
        last_triaged_ts=th.get("last_triaged_ts"),
        status=th.get("status"),
        triage_kind=th.get("triage_kind"),
        triage_reason=th.get("triage_reason"),
        assertion_ids=th.get("assertion_ids") or [],
        **kw,
    )


@router.get("/captures")
async def captures(
    console: ConsoleDep, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[CaptureSummary]:
    """Threads the capture loop has seen, newest first, with its decision on each."""
    return [
        await _summary(
            r["t"],
            console,
            r["first"],
            started_at=r["started_at"],
            messages=r["n"],
            has_trace=r["has_trace"],
        )
        for r in await console.queries.threads(limit)
    ]


@router.get("/captures/{thread_id}")
async def capture(thread_id: str, console: ConsoleDep) -> CaptureDetail:
    """One thread: its messages, and what each pipeline step and gate decided."""
    row = await console.queries.thread(thread_id)
    if row is None:
        raise HTTPException(404, f"unknown thread {thread_id!r}")
    t = row["t"]
    messages = [
        ThreadMessage(
            event_id=m["event_id"],
            author=await console.names.user(m.get("author")),
            event_time=m["event_time"],
            text=await _clean(m.get("text") or "", console.names),
            permalink=m.get("permalink"),
        )
        for m in row["messages"]
        if m.get("event_time")
    ]
    raw: CaptureResult | None = None
    if t.get("capture_json"):
        try:
            raw = CaptureResult.model_validate_json(t["capture_json"])
        except ValidationError:
            log.warning("capture_json_unreadable", thread_id=thread_id)
    summary = await _summary(
        t,
        console,
        messages[0].text if messages else None,
        started_at=messages[0].event_time if messages else None,
        messages=len(messages),
        has_trace=raw is not None,
    )
    if raw is not None:
        items = gates.items(raw)
    else:
        ids = list(dict.fromkeys(summary.assertion_ids))  # a thread may list one twice
        items = [gates.stored(v) for v in await console.service.repo.assertions(ids)]
    return CaptureDetail(
        summary=summary,
        messages=messages,
        steps=gates.steps(raw.steps) if raw else [],
        items=items,
        started_at=raw.started_at if raw else None,
        trace_id=raw.trace_id if raw else None,
        raw=raw,
    )


def _title(calls: list[ToolCallView]) -> str:
    for c in calls:
        for key in ("text", "query", "question"):
            if isinstance(v := c.args.get(key), str) and v:
                return short(v, 70)
    for c in calls:
        for key in ("entity_id", "entity_or_site", "assertion_id"):
            if isinstance(v := c.args.get(key), str) and v:
                return f"{c.tool.replace('_', ' ')} · {v}"
    return calls[0].tool.replace("_", " ")


@router.get("/agent-runs")
async def agent_runs(
    console: ConsoleDep, hours: Annotated[int, Query(ge=1, le=24 * 30)] = 72
) -> list[AgentRun]:
    """MCP tool calls by any agent, grouped into runs: one agent, no pause over 90 s."""
    calls = await console.queries.tool_calls(datetime.now(UTC) - timedelta(hours=hours))
    runs: list[list[ToolCallView]] = []
    open_run: dict[str, list[ToolCallView]] = {}
    for c in calls:
        cur = open_run.get(c.agent_id)
        if cur is None or c.at - cur[-1].at > RUN_GAP:
            cur = []
            runs.append(cur)
            open_run[c.agent_id] = cur
        cur.append(c)
    out = [
        AgentRun(
            run_id=r[0].call_id,
            agent_id=r[0].agent_id,
            title=_title(r),
            started_at=r[0].at,
            ended_at=r[-1].at,
            calls=r,
            writes=[c.tool for c in r if c.tool in WRITE_TOOLS],
        )
        for r in runs
    ]
    return sorted(out, key=lambda r: r.started_at, reverse=True)


@router.get("/eval")
async def eval_report(console: ConsoleDep) -> EvalReportView | None:
    """The latest `silentctx eval` report, or null if none has run."""
    folder = console.settings.eval_results_dir
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    for path in reversed(files):
        try:
            report = EvalReportView.model_validate_json(path.read_text("utf-8"))
        except (ValidationError, OSError, json.JSONDecodeError):
            log.warning("eval_report_unreadable", file=str(path))
            continue
        return report.model_copy(update={"file": path.name})
    return None


def diff(prev: dict[str, Any], cur: dict[str, Any]) -> Change | None:
    pa, ca = prev["assertions"], cur["assertions"]
    pt, ct = prev["threads"], cur["threads"]
    new_a = [k for k in ca if k not in pa]
    new_t = [k for k, v in ct.items() if pt.get(k) != v]
    change = Change(
        assertions=ca != pa or cur["relations"] != prev["relations"],
        captures=ct != pt,
        agents=cur["calls"] != prev["calls"],
        new_assertions=new_a,
        new_captures=new_t,
    )
    return change if change.assertions or change.captures or change.agents else None


@router.get("/stream", response_class=EventSourceResponse)
async def stream(request: Request, console: ConsoleDep) -> EventSourceResponse:
    """Server-sent events: `change` whenever the graph, captures or agent calls change.
    The browser refetches what changed and highlights new knowledge."""

    async def events() -> AsyncIterator[dict[str, str]]:
        prev: dict[str, Any] | None = None
        while not await request.is_disconnected():
            try:
                cur = await console.queries.snapshot()
            except Exception as exc:  # noqa: BLE001  # Neo4j blip: try again next tick
                log.warning("console_snapshot_failed", error=f"{type(exc).__name__}: {exc}")
            else:
                if prev is None:
                    yield {"event": "ready", "data": "{}"}
                elif (change := diff(prev, cur)) is not None:
                    yield {"event": "change", "data": change.model_dump_json()}
                prev = cur
            await asyncio.sleep(console.settings.ui_poll_seconds)

    return EventSourceResponse(events(), ping=15)
