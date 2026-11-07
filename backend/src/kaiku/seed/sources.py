"""Read and validate the mock_data files. Any malformed row fails loudly here, before a
single write reaches Neo4j."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from kaiku.logging import get_logger
from kaiku.seed.models import (
    AccessRecordRow,
    AlarmRow,
    DeviceEventRow,
    DeviceRow,
    EntityRow,
    GoldAssertionRow,
    LinkRow,
    NodeRunningRow,
    SegmentRow,
    SnapshotRow,
)

log = get_logger(__name__)


def load_aliases(data_dir: Path) -> dict[str, str]:
    """Surface forms for entity resolution (knowledge_graph/entity_aliases.csv), or none
    when the data directory is not a mock_data checkout: the api and the stdio server
    still start, with id-only resolution."""
    try:
        return MockData(data_dir).model_aliases()
    except (FileNotFoundError, KeyError) as exc:
        log.warning("entity_aliases_unavailable", error=str(exc))
        return {}


def _jsonl(path: Path) -> Iterator[Any]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def _load[RowT: BaseModel](model: type[RowT], path: Path) -> list[RowT]:
    raw = list(_jsonl(path)) if path.suffix == ".jsonl" else json.loads(path.read_text("utf-8"))
    return TypeAdapter(list[model]).validate_python(raw)  # type: ignore[valid-type]


class MockData:
    """Paths into a mock_data checkout (DATA_DIR)."""

    def __init__(self, root: Path) -> None:
        if not (root / "network").is_dir():
            raise FileNotFoundError(f"{root} is not a mock_data directory (no network/)")
        self.root = root

    def entities(self) -> list[EntityRow]:
        return _load(EntityRow, self.root / "knowledge_graph" / "entities.jsonl")

    def devices(self) -> list[DeviceRow]:
        return _load(DeviceRow, self.root / "network" / "as_built" / "devices.json")

    def nodes_running(self) -> list[NodeRunningRow]:
        return _load(NodeRunningRow, self.root / "network" / "as_running" / "nodes_current.json")

    def segments(self) -> list[SegmentRow]:
        return _load(SegmentRow, self.root / "network" / "as_built" / "segments.json")

    def links(self) -> list[LinkRow]:
        running = self.root / "network" / "as_running"
        return _load(LinkRow, running / "links_current.json") + _load(
            LinkRow, running / "links_retired.json"
        )

    def snapshots(self) -> list[SnapshotRow]:
        path = self.root / "network" / "as_running" / "physical_node_snapshots.json"
        return _load(SnapshotRow, path)

    def device_events(self) -> list[DeviceEventRow]:
        return _load(DeviceEventRow, self.root / "network" / "events" / "device_events.jsonl")

    def alarms(self) -> list[AlarmRow]:
        return _load(AlarmRow, self.root / "network" / "alarms" / "alarms.jsonl")

    def gold_assertions(self) -> list[GoldAssertionRow]:
        return _load(GoldAssertionRow, self.root / "knowledge_graph" / "assertions.jsonl")

    def access_register(self) -> list[AccessRecordRow]:
        return _load(AccessRecordRow, self.root / "knowledge_graph" / "access_register.jsonl")

    def model_aliases(self) -> dict[str, str]:
        """Lower-cased surface form -> canonical entity id (e.g. "tnc1x" -> "TNC-1X")."""
        path = self.root / "knowledge_graph" / "entity_aliases.csv"
        with path.open(encoding="utf-8", newline="") as fh:
            return {
                row["alias_surface"].strip().casefold(): row["canonical_entity"].strip()
                for row in csv.DictReader(fh)
            }

    def teams_messages(self) -> Iterator[dict[str, Any]]:
        for path in sorted((self.root / "teams" / "messages").glob("*.jsonl")):
            yield from _jsonl(path)

    def mail_index(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = json.loads(
            (self.root / "email_outlook" / "mail_export_index.json").read_text("utf-8")
        )
        return rows
