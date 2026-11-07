"""Shared domain types. Names match `mock_data/knowledge_graph/assertion_schema.json`."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# Every instant in the domain is timezone-aware and normalised to UTC (plan 001 R8).
# Naive datetimes are rejected at the model boundary.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


class AssertionStatus(StrEnum):
    UNVERIFIED = "unverified"
    BELIEVED_TRUE = "believed_true"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class Platform(StrEnum):
    """Where an event's `event_time` comes from (plan 001 R1). Every stored `platform`
    string is one of these."""

    SLACK = "slack"
    TEAMS = "teams"
    EMAIL = "email"
    ALARM = "alarm"  # the alarm's raise time
    DEVICE_EVENT = "device_event"  # the as-running change (firmware upgrade, decommission)
    PDF = "pdf"  # an expert's notes: the PDF CreationDate
    CONFLUENCE = "confluence"  # a wiki page: when it was last edited
    ITSM = "itsm"  # a ticketing KB article: when it was published
    MCP = "mcp"  # an agent's write: the outcome's observed time, the action's wall clock
    NETWORK = "network"  # the telemetry check (V4) at the reported observation time
    SIM = "sim"  # the simulator's fault and action events
