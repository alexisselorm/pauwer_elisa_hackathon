"""The MCP server: the only interface for agents, ours included (plan 002 §4).

Served by the API process at `/mcp` (Streamable HTTP). Each agent authenticates with its
own bearer token from `MCP_TOKENS` (agent_id -> token); the agent id on every write comes
from the token, never from the request (plan 001 §7).

Read tools: resolve_entities, get_entity, get_context, search_context, check_conditions,
get_site_access, get_events, get_history. Write tools: report_outcome, open_knowledge_gap,
propose_assertion, flag_assertion. Deliberately absent: confirm, retract, delete, or set
validity. Prompt `ops_triage`; resource `context://entity/{entity_id}`.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr

from silentctx import __version__
from silentctx.context.models import (
    ConditionsResult,
    ContextResult,
    EntityView,
    EventsResult,
    FlagRecorded,
    GapOpened,
    HistoryResult,
    OutcomeRecorded,
    OutcomeResult,
    ProposalCreated,
    ResolveResult,
    SearchResult,
    SiteAccessResult,
)
from silentctx.context.service import ContextError, ContextService
from silentctx.domain.models import AssertionStatus
from silentctx.logging import get_logger

SERVER_NAME = "silentctx"
INSTRUCTIONS = (
    "Operational context layer for a telecom transport network: knowledge captured from "
    "experts' chat and email, bound to network entities (devices, hardware models, segments) "
    "and valid over time. Resolve entities first, then read context as of a time. Cite "
    "assertion ids. Times are ISO-8601 UTC; as_of and known_at default to now."
)
OPS_TRIAGE_PROMPT = """You are a network operations assistant for a telecom transport network.
You answer through the silentctx context layer only. Rules:
1. Resolve what the user is talking about with resolve_entities (or get_entity if you have an id).
   If that finds nothing, call search_context with the question before asking the user.
2. Before recommending anything, call get_context for the entity. If an assertion lists
   symptoms, call check_conditions for it on that entity and report each condition as met,
   not met, or unknown. Never claim a condition is met when it is unknown.
3. Never recommend a step without citing the assertion id it comes from. Respect `avoid`.
   If a condition is not met, don't recommend that assertion's action.
4. If assertions contradict each other or are disputed, show both views with their dates.
5. If nothing relevant is documented, say "not documented" and call open_knowledge_gap with
   the user's question and the entity ids. Don't guess.
6. For site visits, call get_site_access with as_of = the planned visit time.
7. After any action is taken, call report_outcome.
Keep answers short and practical: the next step, what to avoid, and why (with sources)."""

log = get_logger(__name__)

When = Annotated[
    datetime | None,
    Field(description="ISO-8601 UTC instant; defaults to now"),
]
EntityId = Annotated[str, Field(description="graph entity id, e.g. TRN-017, TNC-1X, SEG-042")]
OnBehalfOf = Annotated[
    str | None, Field(description="the person the agent acted for, e.g. a Slack user")
]
TriggerEventId = Annotated[
    str | None, Field(description="the event that prompted this write, e.g. an alarm id")
]
IdempotencyKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        description="unique per intended write; repeating it returns the first result",
    ),
]


class PingResult(BaseModel):
    server: str
    version: str
    agent_id: str | None


class StaticTokenVerifier:
    """Maps a bearer token to its agent_id. Constant-time comparison."""

    def __init__(self, tokens: dict[str, SecretStr]) -> None:
        self._tokens = [(agent_id, t.get_secret_value().encode()) for agent_id, t in tokens.items()]

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = token.encode()
        match: str | None = None
        # Compare against every token so the timing doesn't reveal which one matched.
        for agent_id, expected in self._tokens:
            if hmac.compare_digest(presented, expected):
                match = agent_id
        if match is None:
            return None
        return AccessToken(token=token, client_id=match, scopes=["context"])


def current_agent_id() -> str | None:
    """The authenticated agent for the current MCP request, if any."""
    access = get_access_token()
    return access.client_id if access else None


class ServiceHolder:
    """The ContextService is built in the app lifespan (it needs the Neo4j driver); the MCP
    server is built before that. Tools look the service up at call time."""

    def __init__(self, stdio_agent_id: str | None = None) -> None:
        self.service: ContextService | None = None
        # stdio has no bearer token: the local operator names the agent explicitly.
        self.stdio_agent_id = stdio_agent_id

    def get(self) -> ContextService:
        if self.service is None:
            raise ToolError("the context layer is starting; retry in a few seconds")
        return self.service


async def _get[T: BaseModel](fn: Callable[[], Awaitable[T]]) -> T:
    try:
        return await fn()
    except ContextError as exc:
        raise ToolError(str(exc)) from exc


async def _call(fn: Callable[[], Awaitable[BaseModel]]) -> CallToolResult:
    """structuredContent plus text: a one-line summary first, then the same data as JSON
    (models read the text; the JSON keeps every id they may cite)."""
    result = await _get(fn)
    return _result(result)


def _result(result: BaseModel) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(type="text", text=summarize(result)),
            TextContent(type="text", text=result.model_dump_json()),
        ],
        structuredContent=result.model_dump(mode="json"),
    )


def _ids(items: list[Any], key: str = "assertion_id", n: int = 5) -> str:
    ids = [getattr(i, key) for i in items[:n]]
    more = f" +{len(items) - n}" if len(items) > n else ""
    return (", ".join(ids) + more) if ids else "none"


def summarize(r: BaseModel) -> str:
    """One short line per tool result."""
    replay = " (replayed)" if getattr(r, "replayed", False) else ""
    match r:
        case PingResult():
            return f"{r.server} {r.version}, agent {r.agent_id or 'unknown'}"
        case ResolveResult():
            best = r.candidates[0] if r.candidates else None
            top = f"; best {best.entity_id} ({best.confidence:.2f})" if best else ""
            return f"{len(r.candidates)} candidate(s) for {r.text!r}{top}"
        case EntityView():
            what = " ".join(x for x in (r.model, r.generation, r.firmware) if x)
            alive = "in service" if r.exists_at else "not in service"
            if r.entity_type in ("hw_model", "generation"):
                alive = f"{len(r.members)} device(s) in service"
            return f"{r.entity_id} ({r.entity_type}) {what} at {r.as_of.isoformat()}: {alive}"
        case ContextResult():
            note = f"; {r.note}" if r.note else ""
            return f"{len(r.assertions)} assertion(s) for {r.entity_id}: {_ids(r.assertions)}{note}"
        case SearchResult():
            return f"{len(r.assertions)} {r.mode} match(es): {_ids(r.assertions)}"
        case ConditionsResult():
            each = ", ".join(f"{c.name} {c.status}" for c in r.conditions) or "no conditions"
            return f"{r.assertion_id} on {r.entity_id}: {each}; all met: {r.all_met}"
        case SiteAccessResult():
            locks = ", ".join(f"{a.lock} ({a.holder})" for a in r.access_register) or "none"
            return f"{len(r.assertions)} access assertion(s) for {r.entity_id}; register: {locks}"
        case EventsResult():
            more = " (truncated)" if r.truncated else ""
            return f"{len(r.events)} event(s) on {r.entity_id}{more}"
        case HistoryResult():
            return (
                f"{r.assertion_id}: {len(r.versions)} version(s), {len(r.contradicts)} "
                f"contradiction(s), {len(r.evidence)} evidence, {len(r.flags)} flag(s)"
            )
        case OutcomeRecorded():
            return f"outcome {r.event_id}{replay}: evidence {r.evidence or 'not counted'}"
        case GapOpened():
            return f"gap {r.gap_id}{replay} routed to {r.routed_to or 'the channel'}"
        case ProposalCreated():
            return f"proposal {r.assertion_id}{replay}: {r.status}"
        case FlagRecorded():
            return f"flag {r.flag_id}{replay} on {r.assertion_id}: review due"
    return type(r).__name__


def _agent(holder: ServiceHolder) -> str:
    agent = current_agent_id() or holder.stdio_agent_id
    if agent is None:  # pragma: no cover - the auth middleware rejects these first
        raise ToolError("unauthenticated")
    return agent


def create_mcp_server(tokens: dict[str, SecretStr], holder: ServiceHolder | None = None) -> FastMCP:
    """A fresh server per app: a session manager can only be run once."""
    holder = holder or ServiceHolder()
    server = FastMCP(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        token_verifier=StaticTokenVerifier(tokens),
        auth=AuthSettings(
            # Required by the SDK's settings model; tokens are static, so there is no OAuth
            # server and no resource metadata (resource_server_url=None).
            issuer_url=AnyHttpUrl("http://localhost"),
            resource_server_url=None,
            required_scopes=["context"],
        ),
        streamable_http_path="/mcp",
        # Stateless: an API restart (or --reload) doesn't break connected agents.
        stateless_http=True,
        # Every request carries a bearer token, so DNS-rebinding protection (meant for
        # unauthenticated local servers) would only block `http://api:8000` in compose.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool()
    async def ping() -> Annotated[CallToolResult, PingResult]:
        """Check the connection. Returns the server version and your agent id."""
        agent = current_agent_id() or holder.stdio_agent_id
        return _result(PingResult(server=SERVER_NAME, version=__version__, agent_id=agent))

    # --- Read tools -----------------------------------------------------------------------

    @server.tool()
    async def resolve_entities(
        text: Annotated[str, Field(description="free text, e.g. 'the old Pori node'")],
        as_of: When = None,
    ) -> Annotated[CallToolResult, ResolveResult]:
        """Find the graph entities a piece of text refers to, with a confidence each."""
        return await _call(lambda: holder.get().resolve_entities(text, as_of))

    @server.tool()
    async def get_entity(
        entity_id: EntityId, as_of: When = None
    ) -> Annotated[CallToolResult, EntityView]:
        """As-built and as-running facts about an entity at a time: model, generation,
        firmware, status, segments and links. For a hardware model or generation, `members`
        lists its devices in service then (e.g. how many nodes are Gen1)."""
        return await _call(lambda: holder.get().get_entity(entity_id, as_of))

    @server.tool()
    async def get_context(
        entity_id: EntityId,
        as_of: When = None,
        known_at: Annotated[
            datetime | None,
            Field(description="only knowledge on record at this instant; defaults to now"),
        ] = None,
        topic: Annotated[str | None, Field(description="filter by topic substring")] = None,
        symptoms: Annotated[
            list[str] | None, Field(description="e.g. ['counters_zero', 'oper_up']")
        ] = None,
        statuses: list[AssertionStatus] | None = None,
    ) -> Annotated[CallToolResult, ContextResult]:
        """Expert knowledge in scope for an entity at a time (node, its model and generation,
        its segments, and observations on devices of the same model), ranked, with validity,
        status, support/refute counts, qualifiers (symptoms, action, avoid) and citations."""
        return await _call(
            lambda: holder.get().get_context(entity_id, as_of, known_at, topic, symptoms, statuses)
        )

    @server.tool()
    async def search_context(
        query: Annotated[str, Field(description="natural-language question or keywords")],
        as_of: When = None,
        entity_id: Annotated[
            str | None, Field(description="limit the search to this entity's scope")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> Annotated[CallToolResult, SearchResult]:
        """Semantic search over knowledge valid at a time (keyword search if embeddings are
        unavailable)."""
        return await _call(lambda: holder.get().search_context(query, as_of, entity_id, limit))

    @server.tool()
    async def check_conditions(
        assertion_id: str,
        entity_id: EntityId,
        port: Annotated[str | None, Field(description="e.g. 1/1/3; optional")] = None,
        at: When = None,
    ) -> Annotated[CallToolResult, ConditionsResult]:
        """Check an assertion's symptoms against telemetry on an entity, using the last
        complete 15-minute interval before `at`: each is met, not_met or unknown."""
        return await _call(lambda: holder.get().check_conditions(assertion_id, entity_id, port, at))

    @server.tool()
    async def get_site_access(
        entity_or_site: EntityId,
        as_of: Annotated[
            datetime | None, Field(description="the planned visit time; defaults to now")
        ] = None,
        known_at: When = None,
    ) -> Annotated[CallToolResult, SiteAccessResult]:
        """How to physically get to a device or segment at the visit time: access knowledge
        (locks, keys, gates, roads) and the access register."""
        return await _call(lambda: holder.get().get_site_access(entity_or_site, as_of, known_at))

    @server.tool()
    async def get_events(
        entity_id: EntityId,
        start: Annotated[datetime, Field(description="ISO-8601 UTC, inclusive")],
        end: Annotated[datetime, Field(description="ISO-8601 UTC, exclusive")],
        types: Annotated[
            list[str] | None,
            Field(description="kinds (Alarm, DeviceEvent, Outcome) or types (LOS, field_visit)"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> Annotated[CallToolResult, EventsResult]:
        """The event stream for an entity: alarms, device events and outcomes."""
        return await _call(lambda: holder.get().get_events(entity_id, start, end, types, limit))

    @server.tool()
    async def get_history(assertion_id: str) -> Annotated[CallToolResult, HistoryResult]:
        """How a piece of knowledge changed: versions (supersession), contradictions,
        corroborations, outcome evidence and flags."""
        return await _call(lambda: holder.get().get_history(assertion_id))

    # --- Write tools (governed: evidence, gaps, proposals, flags only) ---------------------

    @server.tool()
    async def report_outcome(
        entity_id: EntityId,
        action: Annotated[str, Field(description="what was done, e.g. bounce_interface")],
        result: OutcomeResult,
        idempotency_key: IdempotencyKey,
        assertion_id: Annotated[
            str | None, Field(description="the assertion whose advice was followed")
        ] = None,
        port: str | None = None,
        observed_at: When = None,
        evidence_ref: str | None = None,
        on_behalf_of: OnBehalfOf = None,
        trigger_event_id: TriggerEventId = None,
    ) -> Annotated[CallToolResult, OutcomeRecorded]:
        """Record what happened after an action. Becomes evidence for the assertion:
        resolved supports it; not_resolved refutes it only if its conditions were met."""
        return await _call(
            lambda: holder.get().report_outcome(
                agent_id=_agent(holder),
                entity_id=entity_id,
                action=action,
                result=result,
                observed_at=observed_at,
                idempotency_key=idempotency_key,
                assertion_id=assertion_id,
                port=port,
                evidence_ref=evidence_ref,
                on_behalf_of=on_behalf_of,
                trigger_event_id=trigger_event_id,
            )
        )

    @server.tool()
    async def open_knowledge_gap(
        question: Annotated[str, Field(description="the question nobody has documented")],
        entity_ids: Annotated[list[str], Field(min_length=1)],
        idempotency_key: IdempotencyKey,
        context: str | None = None,
        on_behalf_of: OnBehalfOf = None,
        trigger_event_id: TriggerEventId = None,
    ) -> Annotated[CallToolResult, GapOpened]:
        """Record that something isn't documented; it is routed to the most likely expert."""
        return await _call(
            lambda: holder.get().open_knowledge_gap(
                agent_id=_agent(holder),
                question=question,
                entity_ids=entity_ids,
                idempotency_key=idempotency_key,
                context=context,
                on_behalf_of=on_behalf_of,
                trigger_event_id=trigger_event_id,
            )
        )

    @server.tool()
    async def propose_assertion(
        statement: str,
        entity_ids: Annotated[list[str], Field(min_length=1)],
        evidence_event_ids: Annotated[
            list[str], Field(min_length=1, description="existing events this is based on")
        ],
        rationale: str,
        idempotency_key: IdempotencyKey,
        topic: str | None = None,
        qualifiers: dict[str, Any] | None = None,
        valid_from: When = None,
        valid_to: Annotated[datetime | None, Field(description="exclusive end, if any")] = None,
        on_behalf_of: OnBehalfOf = None,
        trigger_event_id: TriggerEventId = None,
    ) -> Annotated[CallToolResult, ProposalCreated]:
        """Propose new knowledge. It is stored as unverified until a person confirms it."""
        return await _call(
            lambda: holder.get().propose_assertion(
                agent_id=_agent(holder),
                statement=statement,
                entity_ids=entity_ids,
                evidence_event_ids=evidence_event_ids,
                rationale=rationale,
                idempotency_key=idempotency_key,
                topic=topic,
                qualifiers=qualifiers,
                valid_from=valid_from,
                valid_to=valid_to,
                on_behalf_of=on_behalf_of,
                trigger_event_id=trigger_event_id,
            )
        )

    @server.tool()
    async def flag_assertion(
        assertion_id: str,
        reason: str,
        idempotency_key: IdempotencyKey,
        evidence_event_ids: list[str] | None = None,
        on_behalf_of: OnBehalfOf = None,
        trigger_event_id: TriggerEventId = None,
    ) -> Annotated[CallToolResult, FlagRecorded]:
        """Ask for a person to review a piece of knowledge (sets review_due)."""
        return await _call(
            lambda: holder.get().flag_assertion(
                agent_id=_agent(holder),
                assertion_id=assertion_id,
                reason=reason,
                evidence_event_ids=evidence_event_ids or [],
                idempotency_key=idempotency_key,
                on_behalf_of=on_behalf_of,
                trigger_event_id=trigger_event_id,
            )
        )

    # --- Prompt and resource ---------------------------------------------------------------

    @server.prompt(name="ops_triage", description="Rules for answering ops questions")
    def ops_triage() -> str:
        return OPS_TRIAGE_PROMPT

    @server.resource(
        "context://entity/{entity_id}",
        name="entity_context",
        description="Current knowledge about an entity, as Markdown",
        mime_type="text/markdown",
    )
    async def entity_context(entity_id: str) -> str:
        result = await _get(lambda: holder.get().get_context(entity_id))
        return context_markdown(result)

    return server


def context_markdown(result: ContextResult) -> str:
    lines = [f"# {result.entity_id}", f"As of {result.as_of.isoformat()}", ""]
    if result.note:
        lines += [f"_{result.note}_", ""]
    for a in result.assertions:
        lines.append(f"## {a.assertion_id} ({a.status}, {a.match or 'related'})")
        lines.append(a.statement)
        q = a.qualifiers
        if q.get("action"):
            lines.append(f"- Action: {q['action']}")
        if q.get("avoid"):
            lines.append(f"- Avoid: {', '.join(map(str, q['avoid']))}")
        until = a.valid_to.isoformat() if a.valid_to else "still believed true"
        lines.append(f"- Valid: {a.valid_from.isoformat()} → {until}")
        for c in a.citations:
            lines.append(f"- Source: {c.source_type} {c.source_ref} ({c.author or 'unknown'})")
        lines.append("")
    return "\n".join(lines)
