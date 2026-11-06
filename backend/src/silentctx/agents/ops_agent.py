"""The reference ops agent (plan 001 WS5, plan 002 §3.4).

It talks to the context layer through MCP only, with its own bearer token, exactly like any
external agent would: tools from `langchain-mcp-adapters`, rules from the server's
`ops_triage` prompt. The model comes from `CHAT_MODEL` (Featherless by default).

Its answer is a structured `OpsAnswer`. Hard rules are enforced in code after the model has
answered, not only asked for in the prompt (plan 001 WS5):
- no step without a citation to an assertion the tools actually returned;
- no step from an assertion whose conditions were checked and are not met;
- "not documented" carries no steps.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, Field

from silentctx.config import Settings
from silentctx.llm.chat import ModelRole, get_chat_model
from silentctx.logging import get_logger

SERVER = "silentctx"
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


class OpsAnswer(BaseModel):
    """The agent's final answer."""

    status: AnswerStatus
    summary: str = Field(description="the answer in 1-3 plain sentences")
    entity_ids: list[str] = Field(default=[], description="entities the answer is about")
    steps: list[Step] = []
    avoid: list[str] = Field(default=[], description="things not to do, from the knowledge")
    conditions: list[ConditionReport] = []
    sources: list[Source] = []
    gap_id: str | None = Field(default=None, description="set if a knowledge gap was opened")
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


def enforce_rules(answer: OpsAnswer, seen: set[str]) -> OpsAnswer:
    """Hard rules, applied whatever the model said."""
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
    status = answer.status
    if status is AnswerStatus.NOT_DOCUMENTED:
        steps = []
    elif not steps and status is AnswerStatus.ANSWERED and not sources:
        status = AnswerStatus.NOT_DOCUMENTED
    return answer.model_copy(
        update={"steps": steps, "sources": sources, "status": status, "review_notes": notes}
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

    async def ask(self, question: str, *, request_id: str, asker: str | None = None) -> OpsAnswer:
        tools, rules = await self._setup()
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        system = (
            f"{rules}\n\nCurrent time: {now} (UTC). Times you pass to tools are ISO-8601 UTC."
            f"\nIf you open a knowledge gap, use idempotency_key={request_id!r}."
            "\nFinish by returning the OpsAnswer."
        )
        agent = create_agent(
            model=get_chat_model(self._settings, ModelRole.CHAT),
            tools=tools,
            system_prompt=system,
            response_format=ToolStrategy(OpsAnswer),
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
        checked = enforce_rules(answer, seen_assertion_ids(result.get("messages", [])))
        log.info(
            "ops_agent_answered",
            status=checked.status,
            steps=len(checked.steps),
            sources=[s.assertion_id for s in checked.sources],
            tool_calls=sum(1 for m in result.get("messages", []) if isinstance(m, ToolMessage)),
        )
        return checked
