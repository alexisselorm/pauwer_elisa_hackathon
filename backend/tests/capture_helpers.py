"""Shared by the capture tests: Elisa's thread, the inventory as the graph returns it, and a
fake LLM judge that answers what a good model would (the LLM itself is not under test)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from silentctx.context.models import Scope
from silentctx.extraction.models import (
    Extraction,
    Mention,
    Quoted,
    Relation,
    RuleDraft,
    Triage,
    TriageKind,
)
from silentctx.ingest.backfill import elisa_example
from silentctx.ingest.store import MessageView, ThreadView
from silentctx.seed.sources import MockData

ROOT = Path(__file__).resolve().parents[2]
MOCK_DATA = ROOT / "mock_data"
ELISA_DIR = ROOT / "Elisa_SinceAI2026" / "ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026"


def elisa_thread(thread_id: str = "elisa:teams_chat") -> ThreadView:
    chat = [m for m in elisa_example(ELISA_DIR) if m.thread_id == "elisa:teams_chat"]
    return ThreadView(
        thread_id=thread_id,
        channel="elisa-example",
        platform="teams",
        last_message_ts=chat[-1].event_time,
        messages=[MessageView.model_validate(m.model_dump()) for m in chat],
    )


def thread_of(
    messages: list[tuple[datetime, str, str | None, str]],
    thread_id: str = "t1",
    platform: str = "slack",
) -> ThreadView:
    """(time, author, role, text) rows as a thread."""
    return ThreadView(
        thread_id=thread_id,
        channel="C1",
        platform=platform,
        last_message_ts=messages[-1][0],
        messages=[
            MessageView(event_id=f"{thread_id}:{i}", event_time=t, author=a, author_role=r, text=x)
            for i, (t, a, r, x) in enumerate(messages)
        ],
    )


def inventory() -> list[dict[str, Any]]:
    """The entities as `ContextRepository.all_entities` returns them after the seed."""
    rows = [
        {**e.model_dump(exclude={"type"}), "entity_type": e.type}
        for e in MockData(MOCK_DATA).entities()
    ]
    epoch = min(r["valid_from"] for r in rows)
    rows += [
        {"entity_id": g, "entity_type": "generation", "name": g, "valid_from": epoch}
        for g in ("Gen1", "Gen2", "Gen3")
    ]
    return rows


def q(value: str, quote: str) -> Quoted:
    return Quoted(value=value, quote=quote)


ELISA_RULE = RuleDraft(
    statement=(
        "On older (Gen1) transmission units, after an SFP replacement the interface can be UP "
        "with normal optics and zero traffic counters; disable and re-enable the interface "
        "instead of replacing the SFP again or rebooting the unit."
    ),
    topic="forwarding_state_after_sfp_swap",
    action=q("bounce_interface", "Disable the interface, wait a little, then enable it again"),
    conditions=[
        q("oper_up", "the interface comes up normally"),
        q("counters_zero", "If they stay at zero even though the interface is UP"),
    ],
    avoid=[
        q("replace_sfp", "So another SFP?"),
        q("reboot_node", "Don't reboot the whole unit unless that fails"),
    ],
    observed=[
        q("oper_up", "Link UP"),
        q("rx_power_normal", "RX power looks normal"),
        q("counters_zero", "Counters basically zero"),
    ],
    entities=[
        Mention(surface_form="TRN-042", role=Scope.OBSERVED_ON),
        Mention(surface_form="older units", role=Scope.APPLIES_TO),
        Mention(surface_form="Changed the SFP", role=Scope.ACTIVITY),
    ],
)


class FakeJudge:
    """Triage by a keyword, extraction and relations from fixed answers; counts calls."""

    def __init__(
        self,
        rules: list[RuleDraft] | None = None,
        *,
        relevant: bool = True,
        kind: TriageKind = TriageKind.RULE,
        relations: dict[str, str] | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.rules = rules if rules is not None else [ELISA_RULE]
        self.relevant, self.kind = relevant, kind
        self.relations = relations or {}  # assertion_id -> relation; "*" for any
        self.fail = fail
        self.calls: list[str] = []

    async def triage(self, thread: ThreadView) -> Triage:
        self.calls.append("triage")
        if self.fail:
            raise self.fail
        if not self.relevant:
            return Triage(relevant=False, kind=TriageKind.NONE, reason="small talk")
        return Triage(relevant=True, kind=self.kind, reason="symptom -> action rule")

    async def extract(self, thread: ThreadView, kind: TriageKind, topics: list[str]) -> Extraction:
        self.calls.append("extract")
        return Extraction(rules=self.rules)

    async def relate(
        self, draft: RuleDraft, candidates: list[dict[str, Any]], starts: str = ""
    ) -> list[Relation]:
        self.calls.append("relate")
        out = []
        for c in candidates:
            rel = self.relations.get(c["assertion_id"]) or self.relations.get("*")
            if rel:
                out.append(Relation(assertion_id=c["assertion_id"], relation=rel))
        return out
