"""What the LLM returns (structured output) and what the pipeline passes between nodes.

The LLM only judges and quotes. Every field it fills carries a verbatim quote (V1), and code
resolves times (R3/R4), entities (BE-3.2) and the final relation to earlier knowledge (V6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from silentctx.context.models import Scope
from silentctx.ingest.store import ThreadView


class TriageKind(StrEnum):
    RULE = "rule"
    ACCESS = "access"
    CORRECTION = "correction"
    NONE = "none"


class Triage(BaseModel):
    relevant: bool = Field(description="true if the thread holds reusable operational knowledge")
    kind: TriageKind
    reason: str = Field(min_length=1, description="one line: why it was kept or ignored")


class Quoted(BaseModel):
    value: str = Field(description="a short snake_case token, e.g. bounce_interface")
    quote: str = Field(description="the exact words from one message that say it")


class Mention(BaseModel):
    surface_form: str = Field(description="the exact words naming the thing, e.g. 'older units'")
    role: Scope | None = Field(
        default=None,
        description="observed_on: where it happened; applies_to: which models/generations "
        "it holds for; activity: the work being done (e.g. SFP replacement)",
    )


class StatedTimeOut(BaseModel):
    phrase: str = Field(description="the exact date words, e.g. 'from 1 December'")
    tense: str = Field(description="'past' or 'future': which way the date points")


class RuleDraft(BaseModel):
    statement: str = Field(description="the knowledge in one self-contained sentence")
    topic: str = Field(description="snake_case topic; reuse a known topic when one fits")
    action: Quoted | None = Field(
        default=None, description="what to do, as said in an answer (not a question)"
    )
    conditions: list[Quoted] = Field(
        default=[],
        description="when it applies; symptoms as oper_up, rx_power_normal, "
        "counters_zero where they fit",
    )
    avoid: list[Quoted] = Field(
        default=[],
        description="what not to do, including suggestions that were rejected, "
        "e.g. replace_sfp, reboot_node",
    )
    observed: list[Quoted] = Field(
        default=[], description="symptoms someone reported seeing in this thread"
    )
    entities: list[Mention] = []
    valid_from: StatedTimeOut | None = Field(
        default=None, description="only an explicit start date or weekday, else null"
    )
    valid_to: StatedTimeOut | None = Field(
        default=None, description="only an explicit end date, else null"
    )


class Extraction(BaseModel):
    rules: list[RuleDraft] = []


class RelationKind(StrEnum):
    DUPLICATE = "duplicate"  # the same knowledge
    CORROBORATES = "corroborates"  # agrees, from another source
    SUPERSEDES = "supersedes"  # the world changed: the new one replaces it from now on
    CORRECTS = "corrects"  # the earlier statement was wrong all along
    CONTRADICTS = "contradicts"  # they disagree, neither replaces the other
    UNRELATED = "unrelated"


class Relation(BaseModel):
    assertion_id: str
    relation: RelationKind
    reason: str = ""


class Relations(BaseModel):
    relations: list[Relation] = []


class Judge(Protocol):
    """The LLM's part. Tests pass a fake; production uses `extraction.llm.LlmJudge`."""

    async def triage(self, thread: ThreadView) -> Triage: ...

    async def extract(
        self, thread: ThreadView, kind: TriageKind, topics: list[str]
    ) -> Extraction: ...

    async def relate(
        self,
        draft: RuleDraft,
        candidates: list[dict[str, Any]],
        starts: str = "",
        scope: Sequence[Mapping[str, str]] = (),
    ) -> list[Relation]: ...
