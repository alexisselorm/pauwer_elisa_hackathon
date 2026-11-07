"""The reference ops agent (plan 001 WS5, plan 002 §3.4).

It talks to the context layer through MCP only, with its own bearer token, exactly like any
external agent would: tools from `langchain-mcp-adapters`, rules from the server's
`ops_triage` prompt. The model comes from `CHAT_MODEL` (Featherless by default).

Its answer is a structured `OpsAnswer`. Hard rules are enforced in code after the model has
answered, not only asked for in the prompt (plan 001 WS5):
- no step without a citation to an assertion the tools actually returned;
- no step from an assertion whose conditions were checked and are not met;
- "not documented" carries no steps;
- three tool errors in a row fail the run (ToolErrorLimit) instead of answering from nothing;
- a cited assertion that has been superseded is never shown without its successor (R11).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, Field

from kaiku.config import Settings
from kaiku.llm.chat import ModelRole, get_chat_model
from kaiku.logging import get_logger

SERVER = "kaiku"
# Read tools plus the one write an answering agent needs. Outcomes are reported by the
# buttons/automation that ran the action, not by the agent that answers a question.
ALLOWED_TOOLS = frozenset(
    {
        "resolve_entities",
        "get_entity",
        "get_context",
        "search_context",
        "check_conditions",
        "get_site_access",
        "get_events",
        "get_history",
        "open_knowledge_gap",
    }
)
_ASSERTION_ID = re.compile(r"\bASR-[A-Z0-9-]+\b")
log = get_logger(__name__)


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    NOT_DOCUMENTED = "not_documented"
    DISPUTED = "disputed"
    PARTIAL = "partial"
    NEEDS_CLARIFICATION = "needs_clarification"


class Step(BaseModel):
    action: str = Field(description="what to do, imperative and short")
    assertion_ids: list[str] = Field(description="the assertions this step comes from")


class ConditionReport(BaseModel):
    assertion_id: str
    name: str = Field(description="e.g. oper_up, rx_power_normal, counters_zero")
    status: str = Field(description="met, not_met or unknown, exactly as check_conditions said")
    detail: str | None = None


class Source(BaseModel):
    assertion_id: str
    who: str | None = Field(default=None, description="author of the knowledge")
    where: str | None = Field(default=None, description="channel, email, document")
    when: str | None = Field(default=None, description="date it was recorded")
    link: str | None = Field(
        default=None,
        description="the citation's source_ref when it is a URL (a Slack permalink), else null",
    )
    # Filled from the tool's own citation after the model answers (ground_sources):
    ref: str | None = Field(default=None, description="leave empty")
    detail: str | None = Field(default=None, description="leave empty")


class OpsAnswer(BaseModel):
    """The agent's final answer."""

    status: AnswerStatus
    summary: str = Field(description="the answer in 1-3 plain sentences")
    entity_ids: list[str] = Field(default=[], description="entities the answer is about")
    steps: list[Step] = []
    avoid: list[str] = Field(default=[], description="things not to do, from the knowledge")
    conditions: list[ConditionReport] = []
    sources: list[Source] = []
    records: list[str] = Field(
        default=[],
        description="entity ids whose get_entity record a network fact in the answer comes "
        "from (a count, a location, a model); never for how to fix or handle something",
    )
    gap_id: str | None = Field(default=None, description="set if a knowledge gap was opened")
    gap_routed_to: str | None = Field(
        default=None, description="the expert the gap was routed to (from open_knowledge_gap)"
    )
    review_notes: list[str] = Field(
        default=[], description="caveats: disputed, unverified, stale, unknown conditions"
    )


def _collect_ids(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "assertion_id" and isinstance(v, str):
                out.add(v)
            else:
                _collect_ids(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_ids(v, out)
    elif isinstance(value, str):
        out.update(_ASSERTION_ID.findall(value))


def seen_assertion_ids(messages: Iterable[Any]) -> set[str]:
    """Assertion ids that appeared in tool results (what the agent may cite)."""
    ids: set[str] = set()
    for m in messages:
        if not isinstance(m, ToolMessage):
            continue
        content = m.content
        parts = content if isinstance(content, list) else [content]
        for part in parts:
            text = part.get("text") if isinstance(part, dict) else part
            if not isinstance(text, str):
                continue
            try:
                _collect_ids(json.loads(text), ids)
            except json.JSONDecodeError:
                _collect_ids(text, ids)
        if m.artifact:
            _collect_ids(m.artifact, ids)
    return ids


def seen_records(messages: Iterable[Any]) -> set[str]:
    """Entity ids that get_entity actually returned (the records the agent may cite)."""
    ids: set[str] = set()
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == "get_entity" and m.status != "error":
            for payload in _payloads(m):
                if isinstance(payload, dict) and isinstance(payload.get("entity_id"), str):
                    ids.add(payload["entity_id"])
    return ids


def _payloads(m: ToolMessage) -> list[Any]:
    """A tool result's structured parts: its artifact and any JSON text content."""
    out = [m.artifact] if m.artifact else []
    parts = m.content if isinstance(m.content, list) else [m.content]
    for part in parts:
        text = part.get("text") if isinstance(part, dict) else part
        if isinstance(text, str):
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(text))
    return out


def opened_gap(messages: Iterable[Any]) -> dict[str, Any] | None:
    """What open_knowledge_gap actually returned (the model's copy of it isn't trusted)."""
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == "open_knowledge_gap" and m.status != "error":
            found: dict[str, Any] = {}
            for payload in _payloads(m):
                _gap_fields(payload, found)
            if found.get("gap_id"):
                return found
    return None


def _citations(value: Any, out: dict[str, list[dict[str, Any]]]) -> None:
    if isinstance(value, dict):
        aid, cits = value.get("assertion_id"), value.get("citations")
        if isinstance(aid, str) and isinstance(cits, list):
            out.setdefault(aid, []).extend(c for c in cits if isinstance(c, dict))
        for v in value.values():
            _citations(v, out)
    elif isinstance(value, list):
        for v in value:
            _citations(v, out)


def ground_sources(answer: OpsAnswer, messages: Iterable[Any]) -> OpsAnswer:
    """Each cited source as the tools returned it: who, where, when, the reference (a Slack
    permalink becomes the link) and the detail (the quoted message, or a document label).
    The model's own summary of a source is only kept where the tools said nothing."""
    found: dict[str, list[dict[str, Any]]] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            for payload in _payloads(m):
                _citations(payload, found)
    sources = []
    for s in answer.sources:
        cits = found.get(s.assertion_id) or []
        # Prefer the captured message (a permalink) over a backfilled reference.
        c = next((c for c in cits if str(c.get("source_ref", "")).startswith("https://")), None)
        c = c or (cits[0] if cits else None)
        if c is None:
            sources.append(s)
            continue
        ref = c.get("source_ref")
        is_link = isinstance(ref, str) and ref.startswith("https://")
        sources.append(
            s.model_copy(
                update={
                    "who": c.get("author") or s.who,
                    "where": c.get("source_type") or s.where,
                    "when": str(c.get("recorded_at") or s.when or "")[:10] or None,
                    "link": ref if is_link else None,
                    "ref": None if is_link else ref,
                    "detail": c.get("detail"),
                }
            )
        )
    return answer.model_copy(update={"sources": sources})


_HELSINKI = ZoneInfo("Europe/Helsinki")


def _superseded(value: Any, out: dict[str, tuple[str, str | None]]) -> None:
    if isinstance(value, dict):
        aid, succ = value.get("assertion_id"), value.get("superseded_by")
        if isinstance(aid, str) and isinstance(succ, str):
            valid_to = value.get("valid_to")
            out.setdefault(aid, (succ, valid_to if isinstance(valid_to, str) else None))
        for v in value.values():
            _superseded(v, out)
    elif isinstance(value, list):
        for v in value:
            _superseded(v, out)


def _local(iso: str | None) -> str:
    if not iso:
        return "a later date"
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return f"{t.astimezone(_HELSINKI):%Y-%m-%d %H:%M} Helsinki time"


def name_successors(answer: OpsAnswer, messages: Iterable[Any]) -> OpsAnswer:
    """R11 (plan 004): every cited assertion the tools reported as superseded gets a note
    naming its successor and when it took over (the predecessor's valid_to, R4b). Elisa's
    "superseded timestamp", visible in the answer whatever the model wrote."""
    found: dict[str, tuple[str, str | None]] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            for payload in _payloads(m):
                _superseded(payload, found)
    cited = [s.assertion_id for s in answer.sources]
    cited += [i for step in answer.steps for i in step.assertion_ids]
    added = [
        f"{aid} was superseded by {found[aid][0]} from {_local(found[aid][1])}."
        for aid in dict.fromkeys(cited)
        if aid in found
    ]
    # First, so Slack (which shows four notes) never cuts them off.
    notes = added + [n for n in answer.review_notes if n not in added]
    return answer.model_copy(update={"review_notes": notes})


def _gap_fields(value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("gap_id"), str):
            out.setdefault("gap_id", value["gap_id"])
            out.setdefault("routed_to", value.get("routed_to"))
        for v in value.values():
            _gap_fields(v, out)
    elif isinstance(value, list):
        for v in value:
            _gap_fields(v, out)


def enforce_rules(
    answer: OpsAnswer, seen: set[str], records: set[str] | frozenset[str] = frozenset()
) -> OpsAnswer:
    """Hard rules, applied whatever the model said. `records` are the entity ids get_entity
    returned: a network fact may stand on one, a step never does."""
    notes = list(answer.review_notes)
    blocked = {c.assertion_id for c in answer.conditions if c.status == "not_met"}
    steps: list[Step] = []
    for step in answer.steps:
        cited = [i for i in step.assertion_ids if i in seen]
        if not cited:
            notes.append(f"Removed an uncited step: {step.action!r}.")
        elif set(cited) <= blocked:
            notes.append(f"Removed {step.action!r}: its conditions are not met.")
        else:
            steps.append(step.model_copy(update={"assertion_ids": cited}))
    sources = [s for s in answer.sources if s.assertion_id in seen]
    cited_records = [r for r in answer.records if r in records]
    status = answer.status
    if status is AnswerStatus.NOT_DOCUMENTED:
        steps = []
    elif not steps and status is AnswerStatus.ANSWERED and not sources:
        if cited_records:
            notes.append("From the network inventory, not from expert knowledge.")
        else:
            status = AnswerStatus.NOT_DOCUMENTED
    return answer.model_copy(
        update={
            "steps": steps,
            "sources": sources,
            "records": cited_records,
            "status": status,
            "review_notes": notes,
        }
    )


def _from_plain_text(messages: list[Any]) -> OpsAnswer:
    """The model sometimes ends with plain text (usually a clarifying question) instead of
    the structured answer. Keep its words; it carries no steps, so no rule is bypassed."""
    text = next(
        (
            str(m.content).strip()
            for m in reversed(messages)
            if isinstance(m, AIMessage) and str(m.content).strip()
        ),
        "",
    )
    if not text:
        raise RuntimeError("the agent ended without an answer")
    return OpsAnswer(status=AnswerStatus.NEEDS_CLARIFICATION, summary=text)


class ToolErrorsExceeded(RuntimeError):
    """The tools kept failing: the run stops visibly rather than guess (BE-4.3)."""


class ToolErrorLimit(AgentMiddleware):
    """Counts tool errors in a row for one run; a success resets the count."""

    def __init__(self, limit: int = 3) -> None:
        super().__init__()
        self.limit, self.streak = limit, 0

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        result = await handler(request)
        if isinstance(result, ToolMessage) and result.status == "error":
            self.streak += 1
            if self.streak >= self.limit:
                raise ToolErrorsExceeded(
                    f"{self.streak} tool errors in a row (last: {request.tool_call['name']})"
                )
        else:
            self.streak = 0
        return result


class OpsAgent:
    """One per process. Connects lazily: the API may still be starting when the bot does."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = MultiServerMCPClient(
            {
                SERVER: {
                    "transport": "streamable_http",
                    "url": settings.mcp_url,
                    "headers": {
                        "Authorization": f"Bearer {settings.ops_agent_token().get_secret_value()}"
                    },
                }
            }
        )
        self._tools: list[BaseTool] | None = None
        self._rules: str | None = None
        self._lock = asyncio.Lock()

    async def _setup(self) -> tuple[list[BaseTool], str]:
        async with self._lock:
            if self._tools is None or self._rules is None:
                tools = await self._client.get_tools(server_name=SERVER)
                self._tools = [t for t in tools if t.name in ALLOWED_TOOLS]
                prompt = await self._client.get_prompt(SERVER, "ops_triage")
                self._rules = "\n".join(str(m.content) for m in prompt)
                log.info("ops_agent_ready", tools=sorted(t.name for t in self._tools))
            return self._tools, self._rules

    async def ask(
        self,
        question: str,
        *,
        request_id: str,
        asker: str | None = None,
        trigger_event_id: str | None = None,
        now: datetime | None = None,
    ) -> OpsAnswer:
        """`now`: the agent's present (the eval asks each question at its own as_of)."""
        tools, rules = await self._setup()
        current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()
        system = (
            f"{rules}\n\nCurrent time: {current} (UTC). Times you pass to tools are ISO-8601 UTC."
            f"\nIf you open a knowledge gap, use idempotency_key={request_id!r}"
            + (f" and trigger_event_id={trigger_event_id!r}." if trigger_event_id else ".")
            + "\nFinish by returning the OpsAnswer."
        )
        agent = create_agent(
            model=get_chat_model(self._settings, ModelRole.CHAT),
            tools=tools,
            system_prompt=system,
            response_format=ToolStrategy(OpsAnswer),
            middleware=[ToolErrorLimit()],  # per run: the count starts at zero
        )
        content = question if not asker else f"{question}\n\n(asked by {asker})"
        result = await asyncio.wait_for(
            agent.ainvoke(
                {"messages": [{"role": "user", "content": content}]},
                config={"recursion_limit": 30},
            ),
            timeout=self._settings.ops_agent_timeout_s,
        )
        answer = result.get("structured_response")
        if not isinstance(answer, OpsAnswer):
            answer = _from_plain_text(result.get("messages", []))
        messages = result.get("messages", [])
        checked = ground_sources(
            enforce_rules(answer, seen_assertion_ids(messages), seen_records(messages)), messages
        )
        checked = name_successors(checked, messages)
        gap = opened_gap(messages)
        if gap is not None:
            checked = checked.model_copy(
                update={"gap_id": gap["gap_id"], "gap_routed_to": gap.get("routed_to")}
            )
        log.info(
            "ops_agent_answered",
            status=checked.status,
            steps=len(checked.steps),
            sources=[s.assertion_id for s in checked.sources],
            records=checked.records,
            tool_calls=sum(1 for m in messages if isinstance(m, ToolMessage)),
        )
        return checked

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one MCP tool directly, as this agent (the Slack buttons report outcomes this
        way: through MCP like any agent, never by importing the repository)."""
        async with self._client.session(SERVER) as session:
            result = await session.call_tool(name, arguments)
        text = " ".join(getattr(c, "text", "") for c in result.content).strip()
        if result.isError:
            raise RuntimeError(f"{name} failed: {text or 'tool error'}")
        return dict(result.structuredContent or {})
