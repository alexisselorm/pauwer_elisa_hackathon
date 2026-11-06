"""What the context layer returns to agents (MCP tool outputs, plan 002 §4).

All instants are UTC ISO-8601. These models are the public contract: any MCP client sees
them as the tools' output schemas.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from silentctx.domain.models import AssertionStatus, UtcDatetime
from silentctx.telemetry.store import ConditionResult


class Scope(StrEnum):
    """How an assertion is bound to an entity (plan 001 §5)."""

    OBSERVED_ON = "observed_on"
    APPLIES_TO = "applies_to"
    ACTIVITY = "activity"


class Match(StrEnum):
    """Why an assertion is in scope for the entity asked about (ranking order)."""

    DIRECT = "direct"  # bound to this entity
    MODEL = "model"  # bound to its hardware model or generation
    SEGMENT = "segment"  # bound to a segment that connects it (site access, roads)
    SAME_MODEL_DEVICE = "same_model_device"  # observed on another device of the same model


# --- Entities -----------------------------------------------------------------------------------


class EntityCandidate(BaseModel):
    entity_id: str
    entity_type: str
    name: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    matched: str = Field(description="the text that matched")


class ResolveResult(BaseModel):
    text: str
    as_of: UtcDatetime
    candidates: list[EntityCandidate]


class SegmentRef(BaseModel):
    entity_id: str
    name: str | None = None
    private_road: bool = False
    barrier: str | None = None


class EntityView(BaseModel):
    entity_id: str
    entity_type: str
    name: str | None = None
    as_of: UtcDatetime
    exists_at: bool
    valid_from: UtcDatetime | None = None
    valid_to: UtcDatetime | None = None
    model: str | None = None
    generation: str | None = None
    firmware: str | None = Field(
        default=None, description="firmware at as_of when known from upgrade events"
    )
    firmware_source: str | None = None
    running_hw_model: str | None = Field(
        default=None, description="hardware model in the as-running snapshot at as_of"
    )
    status: str | None = None
    location: str | None = None
    segments: list[SegmentRef] = []
    linked_devices: list[str] = []
    properties: dict[str, Any] = {}


# --- Knowledge ---------------------------------------------------------------------------------


class Citation(BaseModel):
    source_type: str | None = None
    source_ref: str | None = None
    detail: str | None = None
    author: str | None = None
    recorded_at: UtcDatetime | None = None


class Binding(BaseModel):
    entity_id: str
    scope: Scope


class AssertionView(BaseModel):
    assertion_id: str
    statement: str
    topic: str | None = None
    status: AssertionStatus
    origin: str | None = None
    confidence: str | float | None = None
    valid_from: UtcDatetime
    valid_to: UtcDatetime | None = None
    recorded_at: UtcDatetime
    review_due: bool = False
    support_n: int = 0
    refute_n: int = 0
    qualifiers: dict[str, Any] = {}
    bindings: list[Binding] = []
    citations: list[Citation] = []
    contradicts: list[str] = []
    corroborates: list[str] = []
    superseded_by: str | None = None
    supersedes: str | None = None
    match: Match | None = None
    score: float | None = None


class ContextResult(BaseModel):
    entity_id: str
    as_of: UtcDatetime
    known_at: UtcDatetime
    entity_exists: bool
    scope_entities: list[str] = Field(description="the entity, its model, generation, segments")
    assertions: list[AssertionView]
    note: str | None = None


class SearchResult(BaseModel):
    query: str
    as_of: UtcDatetime
    mode: str = Field(description="'semantic' or 'keyword' (when embeddings are unavailable)")
    assertions: list[AssertionView]


class ConditionsResult(BaseModel):
    assertion_id: str
    entity_id: str
    port: str | None
    at: UtcDatetime
    conditions: list[ConditionResult]
    all_met: bool
    note: str | None = None


class AccessRecordView(BaseModel):
    segment_id: str
    barrier: str | None = None
    lock: str | None = None
    holder: str | None = None
    where: str | None = None
    valid_from: UtcDatetime
    valid_to: UtcDatetime | None = None
    recorded_at: UtcDatetime


class SiteAccessResult(BaseModel):
    entity_id: str
    as_of: UtcDatetime
    known_at: UtcDatetime
    segments: list[SegmentRef]
    assertions: list[AssertionView]
    access_register: list[AccessRecordView]
    note: str | None = None


class EventView(BaseModel):
    event_id: str
    kind: str = Field(description="Alarm, DeviceEvent, Outcome, AgentAction, Message, Email")
    event_time: UtcDatetime
    type: str | None = None
    severity: str | None = None
    text: str | None = None
    entity_id: str | None = None
    details: dict[str, Any] = {}


class EventsResult(BaseModel):
    entity_id: str
    start: UtcDatetime
    end: UtcDatetime
    events: list[EventView]
    truncated: bool = False


class EvidenceView(BaseModel):
    event_id: str
    relation: str  # SUPPORTS | REFUTES
    event_time: UtcDatetime
    result: str | None = None
    entity_id: str | None = None
    agent_id: str | None = None
    conditions_met: bool | None = None


class FlagView(BaseModel):
    flag_id: str
    reason: str
    agent_id: str | None = None
    flagged_at: UtcDatetime


class HistoryResult(BaseModel):
    assertion_id: str
    versions: list[AssertionView] = Field(description="the supersession chain, oldest first")
    contradicts: list[AssertionView]
    corroborates: list[AssertionView]
    evidence: list[EvidenceView]
    flags: list[FlagView]


# --- Writes --------------------------------------------------------------------------------------


class OutcomeResult(StrEnum):
    RESOLVED = "resolved"
    NOT_RESOLVED = "not_resolved"
    UNKNOWN = "unknown"


class OutcomeRecorded(BaseModel):
    event_id: str
    assertion_id: str | None
    evidence: str | None = Field(description="SUPPORTS, REFUTES, or null if not counted")
    conditions_met: bool | None
    support_n: int | None = None
    refute_n: int | None = None
    review_due: bool | None = None
    replayed: bool = Field(default=False, description="true if this idempotency key was seen")


class GapOpened(BaseModel):
    gap_id: str
    question: str
    entity_ids: list[str]
    routed_to: str | None
    replayed: bool = False


class ProposalCreated(BaseModel):
    assertion_id: str
    status: AssertionStatus
    valid_from: UtcDatetime
    valid_to: UtcDatetime | None
    recorded_at: UtcDatetime
    bindings: list[Binding]
    replayed: bool = False


class FlagRecorded(BaseModel):
    flag_id: str
    assertion_id: str
    review_due: bool
    routed_to: str | None
    replayed: bool = False
