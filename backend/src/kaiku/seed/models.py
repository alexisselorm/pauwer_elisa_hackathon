"""Typed rows of the mock_data files the seed loader reads. Unknown fields are ignored;
every instant is validated as timezone-aware UTC."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kaiku.domain.models import AssertionStatus, UtcDatetime


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Validity(_Row):
    valid_from: UtcDatetime
    valid_to: UtcDatetime | None = None


class EntityRow(Validity):
    """knowledge_graph/entities.jsonl: the canonical entity list."""

    entity_id: str
    type: Literal["device", "fibre", "microwave", "site", "hw_model", "topic"]
    name: str | None = None
    kind: str | None = None
    vendor: str | None = None
    model: str | None = None
    city: str | None = None
    commissioned: str | None = None
    end_a: str | None = None
    end_b: str | None = None
    private_road: bool | None = None
    role: str | None = None
    lat: float | None = None
    lon: float | None = None
    generation: str | None = None
    label: str | None = None


class DeviceRow(Validity):
    """network/as_built/devices.json (as-built attributes)."""

    device_id: str
    serial: str | None = None
    chassis: str | None = None
    firmware: str | None = None
    rack: str | None = None
    room: str | None = None
    status: str | None = None
    warranty_end: str | None = None


class NodeRunningRow(Validity):
    """network/as_running/nodes_current.json."""

    node_id: str
    status: str
    firmware: str | None = None
    generation: str | None = None


class SegmentRow(Validity):
    """network/as_built/segments.json."""

    segment_id: str
    type: str
    end_a: str
    end_b: str
    length_km: float | None = None
    owner: str | None = None
    cable_ref: str | None = None
    private_road: bool = False
    barrier: str | None = None


class LinkRow(Validity):
    """network/as_running/links_current.json and links_retired.json."""

    link_id: str
    end_a: str
    end_b: str
    capacity_gbps: float | None = None
    media: str | None = None
    status: str | None = None
    superseded_by: str | None = None


class SnapshotRow(Validity):
    """network/as_running/physical_node_snapshots.json."""

    physical_node_id: str
    physical_name: str
    hardware_class: str | None = None
    hardware_model: str | None = None
    firmware: str | None = None
    software_config_ref: str | None = None
    owner_team: str | None = None
    supersedes: str | None = None
    confidence: str | None = None


class DeviceEventRow(_Row):
    """network/events/device_events.jsonl (as-observed: one timestamp, R1)."""

    event_id: str
    device_id: str
    type: str
    event_time: UtcDatetime
    record_time: UtcDatetime
    end_time: UtcDatetime | None = None
    workorder_id: str | None = None
    outcome: str | None = None
    description: str | None = None
    team: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _record_time_is_event_time(self) -> Self:
        # Elisa's event stream: "record time = event time".
        if self.record_time != self.event_time:
            raise ValueError(f"{self.event_id}: record_time != event_time")
        return self


class AlarmRow(_Row):
    """network/alarms/alarms.jsonl. The alarm's event_time is its raise time (R1)."""

    alarm_id: str
    ne_id: str
    type: str
    severity: str
    probable_cause: str | None = None
    text: str | None = None
    state: str
    raised: UtcDatetime
    cleared: UtcDatetime | None = None


class GoldAssertionRow(Validity):
    """knowledge_graph/assertions.jsonl (eval only)."""

    assertion_id: str
    subject_entity_id: str
    about_entity_kind: str
    topic: str
    statement: str
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    recorded_at: UtcDatetime
    source_type: str
    source_ref: str
    source_detail: str | None = None
    asserted_by: str | None = None
    confidence: Literal["low", "medium", "high"]
    status: AssertionStatus
    supersedes: str | None = None
    superseded_by: str | None = None
    contradicts: list[str] = Field(default_factory=list)
    corroborates: list[str] = Field(default_factory=list)


class AccessRecordRow(Validity):
    """knowledge_graph/access_register.jsonl (eval only)."""

    segment_id: str
    site: str
    barrier: str | None = None
    lock: str | None = None
    holder: str | None = None
    where: str | None = None
    recorded_at: UtcDatetime
    source: str | None = None
    derived: str | None = None

    @property
    def record_id(self) -> str:
        return f"{self.segment_id}@{self.valid_from.isoformat()}"
