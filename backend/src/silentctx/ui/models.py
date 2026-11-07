"""Response models of the console API (UI-1). Read-only views over the graph."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from silentctx.context.models import AssertionView, EvidenceView, FlagView
from silentctx.domain.models import UtcDatetime
from silentctx.extraction.graph import CaptureResult

NodeKind = Literal[
    "device", "hw_model", "generation", "segment", "site", "topic", "assertion", "source"
]
EdgeRel = Literal[
    "ABOUT",
    "IS_MODEL",
    "OF_GENERATION",
    "CONNECTS",
    "SUPERSEDES",
    "CORROBORATES",
    "CONTRADICTS",
    "DERIVED_FROM",
]


class GraphNode(BaseModel):
    id: str
    kind: NodeKind
    label: str
    sub: str | None = None
    valid_from: UtcDatetime | None = None
    valid_to: UtcDatetime | None = None
    # Assertions
    status: str | None = None
    origin: str | None = None
    topic: str | None = None
    recorded_at: UtcDatetime | None = None
    retracted_at: UtcDatetime | None = None
    support_n: int = 0
    refute_n: int = 0
    review_due: bool = False
    # Sources: where the knowledge was said (a Slack thread, an email, a KB article)
    platform: str | None = None
    # Devices: alarms on record
    alarms: int = 0


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    rel: EdgeRel
    scope: str | None = None
    candidate: bool = False


class GraphOut(BaseModel):
    now: UtcDatetime
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class SourceQuote(BaseModel):
    event_id: str
    platform: str | None = None
    author: str | None = None
    event_time: UtcDatetime | None = None
    quote: str | None = None
    text: str | None = None
    permalink: str | None = None
    thread_id: str | None = None
    channel: str | None = None


class AssertionDetail(BaseModel):
    assertion: AssertionView
    sources: list[SourceQuote]
    versions: list[AssertionView] = Field(description="the supersession chain, oldest first")
    contradicts: list[AssertionView]
    corroborates: list[AssertionView]
    evidence: list[EvidenceView]
    flags: list[FlagView]
    thread_id: str | None = None
    source_detail: str | None = Field(default=None, description="for seeded knowledge")
    asserted_by: str | None = None


class CaptureSummary(BaseModel):
    thread_id: str
    platform: str | None = None
    channel: str | None = None
    title: str
    started_at: UtcDatetime | None = None
    last_message_ts: UtcDatetime | None = None
    last_triaged_ts: UtcDatetime | None = None
    status: str | None = None
    triage_kind: str | None = None
    triage_reason: str | None = None
    assertion_ids: list[str] = []
    messages: int = 0
    has_trace: bool = False


class ThreadMessage(BaseModel):
    event_id: str
    author: str | None = None
    event_time: UtcDatetime
    text: str
    permalink: str | None = None


GateResult = Literal["pass", "flag", "fail", "skip", "info"]


class Gate(BaseModel):
    code: str
    name: str
    result: GateResult
    detail: str = ""


class ItemView(BaseModel):
    """One rule through the pipeline: what was extracted, and what each gate decided."""

    statement: str
    topic: str
    assertion_id: str | None = None
    dropped: str | None = None
    gates: list[Gate]
    quotes: dict[str, list[str]] = Field(
        default_factory=dict, description="condition/action/avoid/observed/entity -> quotes"
    )


class StepView(BaseModel):
    node: str
    ms: int
    runs: int


class CaptureDetail(BaseModel):
    summary: CaptureSummary
    messages: list[ThreadMessage]
    steps: list[StepView] = []
    items: list[ItemView] = []
    started_at: UtcDatetime | None = None
    trace_id: str | None = None
    raw: CaptureResult | None = None


class ToolCallView(BaseModel):
    call_id: str
    agent_id: str
    tool: str
    args: dict[str, Any]
    ok: bool
    summary: str
    ms: int
    at: UtcDatetime
    cited: list[str] = Field(default_factory=list, description="assertion ids in the result")


class AgentRun(BaseModel):
    run_id: str
    agent_id: str
    title: str
    started_at: UtcDatetime
    ended_at: UtcDatetime
    calls: list[ToolCallView]
    writes: list[str] = Field(description="write tools called: report_outcome, ...")


class Meta(BaseModel):
    now: UtcDatetime
    phoenix_url: str | None = None
    phoenix_project: str


class Change(BaseModel):
    """What the live stream says changed since its last message."""

    assertions: bool = False
    captures: bool = False
    agents: bool = False
    new_assertions: list[str] = []
    new_captures: list[str] = []


# --- Eval (read leniently: the report format belongs to `silentctx eval`) -------------------


class EvalCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    passed: bool
    detail: str = ""


class EvalItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    passed: bool
    skipped: bool = False
    note: str = ""
    checks: list[EvalCheck] = []


class EvalSet(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    items: list[EvalItem] = []
    field_accuracy: dict[str, str] = {}


class EvalReportView(BaseModel):
    model_config = ConfigDict(extra="ignore")
    file: str = ""
    started_at: datetime
    finished_at: datetime | None = None
    models: dict[str, str | None] = {}
    graph: str = ""
    caveat: str = ""
    sets: list[EvalSet] = []
